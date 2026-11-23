import tempfile
import unittest
from pathlib import Path

from miniagent.bench import arm_settings
from miniagent.bench_metrics import collaboration_metrics
from miniagent.meter import TaskMeter
from miniagent.studio import Studio
from tests.helpers import settings
from tests.test_team import TEAM


class CommunicationMetricsTest(unittest.TestCase):
    def test_costs_interrupt_latency_and_counts_across_checkpoint(self):
        meter = TaskMeter()
        meter.record({"type":"step", "agent":"dev", "step":1})
        meter.record({"type":"step_interrupted", "agent":"dev"})
        meter.record({"type":"mail_delivered", "sender":"user", "agent":"dev", "urgent":True, "interrupted":True, "latency_s":.25})
        meter.record({"type":"mail_delivered", "sender":"user", "agent":"qa", "urgent":False, "latency_s":3.5})
        meter.record({"type":"mail_delivered","sender":"qa","agent":"dev","latency_s":500})
        meter.record({"type":"tool_result","tool":"ask_teammate","agent":"dev","seconds":15.2})
        meter.record({"type":"model_call","agent":"lead","purpose":"consult","prompt_tokens":37,"output_tokens":7})
        meter.record({"type":"model_call","agent":"qa","purpose":"teammate_answer","prompt_tokens":10,"output_tokens":3})
        meter.record({"type":"silence_notice","agent":"dev"})
        self.assertIn({"kind":"silence_notice","agent":"dev"}, meter.snapshot()["alerts"])
        resumed = TaskMeter()
        resumed.restore(meter.checkpoint())
        resumed.record({"type":"checkin","agent":"dev"})
        snapshot = resumed.snapshot()
        comm = snapshot["communication"]
        self.assertEqual((snapshot["steps"],comm["interrupts"],comm["discarded_steps"]), (0,1,1))
        self.assertEqual(comm["delivery"]["normal"], {"count":1,"p95_s":3.5})
        self.assertEqual(comm["delivery"]["urgent"], {"count":1,"p95_s":.25})
        self.assertEqual((comm["consults"],comm["auxiliary_prompt"],comm["auxiliary_output"]), (1,47,10))
        self.assertEqual(snapshot["alerts"], [])
        self.assertEqual((comm["deliveries"], snapshot["tool_s"]), (3, 0.0))

    def test_old_records_scope_ownership_link_rate_and_arm_switches(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = settings(Path(tmp))
            self.assertTrue(arm_settings(cfg,"team",12).team.team_talk)
            self.assertFalse(arm_settings(cfg,"team-classic",12).team.mailboxes)
            studio = Studio(cfg)
            try:
                gid = studio.create_group("Team",TEAM,"lead","autonomous")["id"]
                store = studio.group(gid)
                rec = {"id":"t-test", "trigger_seq":0}
                store.append("delegation", task_id="before", assignee="dev", task="Edit")
                store.append("delegation", task_id=rec["id"], assignee="dev", task="Edit")
                self.assertIsNone(collaboration_metrics(store,rec)["non_owner_browser_minutes"])
                store.append("delegation", task_id=rec["id"], assignee="dev", task="Edit", step="draft")
                store.append("delegation", task_id=rec["id"], assignee="qa", task="Review", review=True, step="test")
                store.trace(rec["id"], {"type":"tool_call","tool":"update_plan"})
                for agent, seconds in [("qa",120),("dev",30)]:
                    store.trace(rec["id"], {"type":"tool_result","tool":"run_browser","agent":agent,"seconds":seconds})
                row = collaboration_metrics(store,rec)
                self.assertEqual((row["plan_link_rate"],row["update_plan_calls"],row["re_delegations"]), (.6667,1,1))
                self.assertEqual(row["browser_minutes"], {"owner":2.0,"non_owner":.5,"unattributed":0})
            finally:
                studio.close()
