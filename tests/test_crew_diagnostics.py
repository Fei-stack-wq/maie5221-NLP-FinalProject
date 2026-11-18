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
