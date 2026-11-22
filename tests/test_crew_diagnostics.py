import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.crew_diagnostics import RoomDiagnostics
from miniagent.studio_service import StudioService
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


class DiagnosticReportTest(unittest.TestCase):
    def test_empty_queries_keep_their_revision_scope_after_recent_messages_rotate(self):
        missing = tool("find_files", pattern="**/booking.html")
        same_page = tool("list_dir", path=".")
        provider = RecordingProvider([
            missing, missing, tool("find_files", pattern="**/different.html"),
            tool("find_files", pattern="**/*.md", offset=50),
            *[same_page for _ in range(6)],
            tool("delegate_task", assignee="dev", objective="Publish the booking page",
                 deliverable_paths=["booking.html"], remaining_roles=["qa"]),
            write("booking.html", "<h1>Meeting room bookings</h1>"), {"text": "Published."},
            tool("delegate_task", assignee="qa", objective="Read the booking page and document the result",
                 deliverable_paths=["qa.md"], remaining_roles=[]),
            tool("read_file", path="booking.html"), write("qa.md", "The booking heading is present."), {"text": "Read the page."},
            {"text": "Delivered."},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            project = base / "project"
            project.mkdir()
            (project / "notes.md").write_text("Existing file.")
            service = StudioService(base / "runs", Settings(api_key="test-sentinel"))
            self.addCleanup(service.close)
            with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
                run = service.start(request("team", objective="Prepare a booking page and get a QA note",
                                            workspace_path=str(project), conversation=roster("lead", "dev", "qa"),
                                            acceptance_files=["booking.html", "qa.md"]))
                detail = finished(service, run["id"])
            self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
            prompt, offered = provider.requests[10]
            self.assertFalse(any(m["role"] == "assistant" and "booking.html" in str(m) for m in prompt))
            self.assertTrue({"find_files", "list_dir", "delegate_task"} <= set(offered))
            facts = json.loads(next(line.split("：", 1)[1] for line in prompt[-1]["content"].splitlines()
                                    if line.startswith("当前公共版本已核实的空查询：")))
            self.assertEqual({f["filters"]["pattern"] for f in facts}, {"**/booking.html", "**/different.html"})
            self.assertTrue(all(f["scope"] == "public" and f["revision"] == 0 and f["matches"] == 0 for f in facts))
            trace = [json.loads(line) for line in (base / "runs" / run["id"] / "trace.jsonl").read_text().splitlines()]
            persisted = [event["fact"] for event in trace if event["kind"] == "coordinator_fact"]
            self.assertEqual(facts, list(reversed(persisted)))
            # Old query tool messages can survive a publish as history. The
            # host's current-version fact set must no longer assert absence.
            new_state = provider.requests[13][0][-1]["content"]
            self.assertNotIn("当前公共版本已核实的空查询", new_state)
            self.assertIn('"published":true', new_state)
            self.assertIn("booking.html", new_state)

    def test_alternating_public_queries_report_covered_pages_gaps_and_real_dispatch(self):
        class Reader(RecordingProvider):
            def chat(self, messages, tools=None, temperature=0):
                if self.cursor < len(self.script) and "report_offset" in self.script[self.cursor]:
                    offset = self.script[self.cursor]["report_offset"]
                    task = re.search(r'"task_id":"(task-[a-f0-9]+)","path":"findings.md"',
                                     '\n'.join(m.get('content') or '' for m in messages)).group(1)
                    self.script[self.cursor] = tool("read_handoff_report", task_id=task, path="findings.md", offset=offset)
                return super().chat(messages, tools, temperature)

        root_page = tool("read_file", path="z-notes.md", offset=0, limit=80)
        listing = tool("list_dir", path=".")
        provider = Reader([
            tool("delegate_task", assignee="analyst", objective="Examine the issue",
                 deliverable_paths=["findings.md"], remaining_roles=["writer"]),
            tool("run_python", code="assert False, 'red probe'"), write("findings.md", "sample " * 1000),
            {"text":"[BLOCKED] Documented a failed check."},
            root_page, {"report_offset":0}, listing, root_page, {"report_offset":0}, listing,
            tool("read_lines", path="z-notes.md", start_line=1, limit=1),
            {"report_offset":1100}, tool("read_file", path="z-notes.md", offset=70, limit=50),
            tool("delegate_task", assignee="writer", objective="Deliver the correction", remaining_roles=[]),
            write("delivery.md", "Edited and published the requested correction."), {"text":"Wrote correction."},
            {"report_offset":0}, {"text":"[BLOCKED] Original independent check remains open."}])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            project = base / "project"
            project.mkdir()
            for i in range(65):
                (project / f"a-{i}.txt").write_text("fixture")
            (project / "z-notes.md").write_text(("0123456789\n" * 20))
            service = StudioService(base / "runs", Settings(api_key="test-sentinel"))
            self.addCleanup(service.close)
            with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
                run = service.start(request("team", objective="Prepare the corrected delivery", workspace_path=str(project),
                                            conversation=roster("lead", "analyst", "writer"),
                                            acceptance_files=["delivery.md","z-notes.md"]))
                detail = finished(service, run["id"])
            messages, offered = provider.requests[13]
            facts = next(json.loads(line) for line in messages[-1]["content"].splitlines()
                         if line.startswith('{"公开版本":'))
            self.assertEqual(facts["最近查询"], 9)
            self.assertEqual(facts["读到新范围或新结果"], 5)
            self.assertEqual(facts["已覆盖或相同结果"], 4)
            self.assertEqual(facts["公共区缺少的验收文件"], ["delivery.md"])
            self.assertEqual(facts["本轮已实际委派角色"], ["analyst"])
            self.assertEqual(facts["未结独立评审"], [])
            self.assertEqual({row["tool"] for row in facts["重复命中"]},
                             {"read_file", "read_lines", "list_dir", "read_handoff_report"})
            self.assertEqual(offered, provider.requests[4][1])
            self.assertTrue(any(row["kind"] == "member_report" and row["published"]
                                for row in detail["room_events"]))
            self.assertNotIn('{"公开版本":', provider.requests[17][0][-1]["content"])

    def test_red_check_carries_report_to_lead_and_repairer_without_publication(self):
        doc = 'Probe URL was invalid.\n' + 'A' * 1500 + ' MIDDLE_PROBE_EVIDENCE ' + 'B' * 1500 + '\nMalformed record causes HTTP 500.'
        class Reader(RecordingProvider):
            def chat(self, messages, tools=None, temperature=0):
                if self.cursor < len(self.script) and self.script[self.cursor] == {'read_report':True}:
                    task_id = re.search(r'"task_id":"(task-[a-f0-9]+)","path":"findings.md"',
                                        '\n'.join(m.get('content') or '' for m in messages)).group(1)
                    self.script[self.cursor] = tool('read_handoff_report', task_id=task_id, path='findings.md', offset=1450)
                return super().chat(messages, tools, temperature)
        provider = Reader([
            tool('delegate_task', assignee='analyst', objective='Check inputs', deliverable_paths=['findings.md'], remaining_roles=['dev']),
            tool('run_python', code="assert 2 + 2 == 5, 'red probe'"), write('findings.md', doc),
            {'text':'[BLOCKED] red probe'}, {'read_report':True},
            tool('delegate_task', assignee='dev', objective='Repair malformed records', remaining_roles=[]),
            {'read_report':True}, {'text':'[BLOCKED] needs more evidence'}, {'text':'[BLOCKED] unresolved checks'}])
        with tempfile.TemporaryDirectory() as scratch:
            base = Path(scratch).resolve()
            project = base / 'project'; project.mkdir()
            service = StudioService(base / 'runs', Settings(api_key='fake-secret'))
            try:
                with patch('miniagent.studio_service.DeepSeekProvider', return_value=provider):
                    run = service.start(request('team', objective='Independently check product', workspace_path=str(project),
                                                conversation=roster('lead', 'analyst', 'dev')))
                    detail = finished(service, run['id'])
                [qa] = [r for r in detail['room_events'] if r['kind']=='member_report' and r['role']=='analyst']
                self.assertFalse(qa['published'])
                self.assertTrue(qa['handoff']['evidence']['observed_failure'])
                self.assertEqual(qa['disposition'],'blocked'); self.assertEqual(detail['run']['status'],'failed')
                [attachment] = qa['artifacts']
                self.assertEqual(service.artifact(run['id'],attachment['path'])['content'],doc)
                self.assertFalse((project / 'findings.md').exists()); self.assertEqual(detail['artifacts'], [])
                lead, peer = provider.requests[4], provider.requests[6]
                for messages, tools in [lead, peer]:
                    self.assertIn('read_handoff_report', tools)
                    self.assertIn('Malformed record causes HTTP 500.', str(messages))
                for index in (5,7):
                    self.assertIn('MIDDLE_PROBE_EVIDENCE', str(provider.requests[index][0]))
                self.assertEqual(len([r for r in detail['room_events'] if r['kind']=='assignment']), 2)
                # The real archived booking run predates diagnostic receipts.
                # Remove those fields in this fixture while retaining TaskStore
                # and the frozen assignment to verify the legacy continuation.
                parent = service.runs_dir / run['id']
                journal = parent / 'public-room.jsonl'
                rows = [json.loads(line) for line in journal.read_text().splitlines()]
                for row in rows:
                    if row['kind'] == 'member_report' and not row['published']:
                        row['artifacts'] = []
                        row['handoff'].pop('diagnostics', None)
                journal.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                old_graph = journal.read_bytes()
                followup = Reader([{'read_report':True}, {'text':'[BLOCKED] requires independent verification'}])
                conversation = {'parent_run_id':run['id'], 'recipient':'lead', 'intent':'edit'}
                with patch('miniagent.studio_service.DeepSeekProvider', return_value=followup):
                    next_run = service.start(request('team', objective='Review the previous findings', conversation=conversation))
                    finished(service, next_run['id'])
                self.assertIn('read_handoff_report', followup.requests[0][1])
                self.assertIn(qa['task_id'], str(followup.requests[0][0]))
                self.assertIn('MIDDLE_PROBE_EVIDENCE', str(followup.requests[1][0]))
                self.assertEqual(journal.read_bytes(), old_graph)
                self.assertFalse((project / 'findings.md').exists())
            finally:
                service.close()

    def test_receipt_only_exposes_assigned_changed_safe_text_and_is_immutable(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch); assignment = root/'private'; workspace = assignment/'agents/author'; workspace.mkdir(parents=True)
            store = RoomDiagnostics(root,'sensitive-api-value')
            contents = {'unchanged.md':'baseline', 'find.md':'finding', 'key.txt':'sensitive-api-value',
                        'unrequested.md':'private note','product.py':'print(2)', 'large.md':'a' * (256*1024+1)}
            for path, body in contents.items():
                (workspace/path).write_text(body)
            (workspace/'unsafe.md').symlink_to(workspace/'unrequested.md')
            original = {'unchanged.md': hashlib.sha256(b'baseline').hexdigest()}
            [receipt] = store.capture(assignment, 'author', ['unchanged.md','key.txt','find.md','unsafe.md','missing.md','product.py','large.md','../outside.txt'], original,'t1')
            args = {'task_id':'t1','path':'find.md'}
            (workspace/'find.md').write_text('later edits')
            self.assertEqual(json.loads(store.read(None,args).content)['content'], 'finding')
            self.assertFalse(store.read(None,{**args,'task_id':'other'}).ok)
            self.assertFalse(store.read(None,{**args,'path':'unrequested.md'}).ok)
            file = next((root/'handoffs').glob('*/project/find.md')); file.write_text('overwritten')
            self.assertFalse(store.read(None,args).ok)
