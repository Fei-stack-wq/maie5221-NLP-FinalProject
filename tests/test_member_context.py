"""Member work gets current project facts and observed receipts after long failed chats."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioService
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import assign, roster
from tests.test_studio_service import finished, request


class MemberContextTest(unittest.TestCase):
    def test_four_failed_chats_stay_out_of_member_context_and_current_receipts_survive_compaction(self):
        old_claim = "PAST MODEL CLAIM THAT TESTS ARE UNAVAILABLE"
        turns = [{"user":f"earlier approved project scope {i}", "recipient":"lead", "status":"failed",
                  "answer_truncated":False, "answer":(old_claim + " 错误信息。")*2300, "artifacts":[],
                  "events":[{"kind":"member_report","role":"dev","text":old_claim*200, "published":False}]}
                 for i in range(4)]
        turns[0]["events"] += [
            {"kind":"human_question","role":"lead","request_id":"old","text":"Which ledger is authoritative?"},
            {"kind":"human_answer","role":"user","request_id":"old","text":"Use ledger.sql as original data."},
            {"kind":"human_answer","role":"user","request_id":"other","text":"orphaned answer"},
            {"kind":"human_answer","role":"dev","request_id":"old","text":"self-reported decision"}]
        with tempfile.TemporaryDirectory() as d:
            base = Path(d).resolve()
            local = base/"project"
            local.mkdir()
            data = "".join(f"actual-source-line-{i:03d} " + "field_1 " * 24 + "\n" for i in range(1,110))
            (local/"ledger.sql").write_text(data)
            sha = hashlib.sha256(data.encode()).hexdigest()
            svc = StudioService(base/"runs",Settings(api_key="test-sentinel"))
            self.addCleanup(svc.close)
            conversation = svc._conversation
            def history(*args,**kwargs):
                result = conversation(*args,**kwargs)
                return {**result,"turns":turns} if kwargs.get("before") is not None else result
            journal = svc._record_room_event
            def confirm(rid, role, kind, text, **fields):
                result = journal(rid,role,kind,text,**fields)
                if kind == "goal":
                    journal(rid,"lead","human_question","What timezone?", request_id="current")
                    journal(rid,"user","human_answer","Use Europe/Paris for this release.", request_id="current")
                return result
            calls = [assign("dev","inspect the original ledger and write its result")]
            calls += [tool("read_lines",path="ledger.sql",start_line=i,limit=15) for i in (1,16,31,46,61)]
            check = tool("run_python",code="assert open('result.md').read().startswith('The real ledger')\nprint('PASS real ledger note')")
            calls += [tool("write_file",path="result.md",content="The real ledger has been inspected."),check,check,{"text":"saved result.md"},
                      {"text":"verified publication"}]
            provider = RecordingProvider(calls)
            with patch.object(svc,"_conversation",side_effect=history), patch.object(svc,"_record_room_event",side_effect=confirm), \
                    patch("miniagent.studio_service.DeepSeekProvider",return_value=provider):
                launched = svc.start(request("team",objective="Carry forward all ORIGINAL_RECORDS",conversation=roster("lead","dev"),
                                     workspace_path=str(local),acceptance_files=["result.md"],
                                     limits={"max_steps":24,"max_total_tokens":120000,"max_seconds":60}))
                detail = finished(svc, launched["id"])
            self.assertEqual(detail["run"]["status"], "completed",detail["run"])
            first, tools = provider.requests[1]
            prompt = json.dumps(first,ensure_ascii=False)
            for required in ("Use Europe/Paris","What timezone?", "Which ledger is authoritative?",
                             "Use ledger.sql as original data.", "ORIGINAL_RECORDS", "inspect the original ledger"):
                self.assertIn(required,prompt)
            self.assertNotIn(old_claim,prompt)
            self.assertLess(len(prompt.encode()),11000)
            self.assertIn("run_shell",tools)
            final_work = json.dumps(provider.requests[6][0],ensure_ascii=False)
            self.assertIn("actual-source-line-061",final_work)
            self.assertIn(sha,final_work)
            self.assertNotIn(old_claim,final_work)
            close, close_tools = provider.requests[-2]
            self.assertEqual(close_tools,[])
            close = json.dumps(close,ensure_ascii=False)
            for expected in ("Use Europe/Paris", "What timezone?", "Which ledger is authoritative?",
                             "Use ledger.sql as original data.", "ORIGINAL_RECORDS", "群内用户确认的问答"):
                self.assertIn(expected,close)
            for excluded in ("actual-source-line-061","orphaned answer","self-reported decision",old_claim):
                self.assertNotIn(excluded,close)
            events = [json.loads(l) for l in (svc.runs_dir/launched["id"]/"trace.jsonl").read_text().splitlines()]
            compacted = [r for r in events if r["kind"]=="context_compacted" and r.get("level")=="subtask" and r["omitted_groups"]]
            self.assertTrue(compacted)
            receipts = [r["checkpoint"] for r in events if r["kind"]=="member_work_checkpoint"]
            self.assertEqual(receipts[-1]["reads"]["ledger.sql"]["sha256"],sha)
            self.assertEqual(receipts[-1]["reads"]["ledger.sql"]["next_line"],76)
            self.assertEqual(receipts[-1]["reads"]["ledger.sql"]["line_ranges"],[[1,75]])
            self.assertIn("result.md",receipts[-1]["versions"])
