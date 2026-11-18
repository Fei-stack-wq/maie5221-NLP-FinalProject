import hashlib
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_reviews import Review, ReviewQueue, Rule, digest
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


PATH = "delivery.txt"
RULE = {"id":"content", "description":"Shipment label must contain the confirmed warehouse.", "method":"inspect"}


def version(content):
    return digest({PATH:hashlib.sha256(content.encode()).hexdigest()})


def submit(content, verdict, **extra):
    return tool("submit_review", candidate_version=version(content), results=[
        {"criterion_id":"content", "verdict":verdict, "evidence":["file:" + PATH], "reason":"Read the shipment label.", **extra}])


class NativeReviewTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("native-review")
        self.service = StudioService(self.root/"runs", Settings(api_key="test-credential"))
        self.addCleanup(cleanup, self.root)
        self.addCleanup(self.service.close)

    def execute(self, steps):
        provider = RecordingProvider(steps)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.service.start(request("team",conversation=roster("lead","builder","inspector"),
                 objective="Build and independently verify the shipment label.",acceptance_files=[PATH],
                 limits={"max_steps":90}))
            detail = finished(self.service, run["id"])
        return detail, provider

    def start_review(self):
        return [tool("delegate_task", assignee="builder",objective="Write the shipment label.",deliverable_paths=[PATH],
                     remaining_roles=["inspector"]),
                write(PATH, "Shipment destination: old warehouse. Verified dispatch record."), {"text":"First label ready."},
                tool("delegate_task",assignee="inspector",objective="Review the finished label.",remaining_roles=[],
                     review={"candidate":"builder","criteria":[RULE]}),
                tool("read_file", path=PATH)]

    def test_reject_fix_independent_recheck_on_new_bytes(self):
        script = self.start_review()+[
            submit("Shipment destination: old warehouse. Verified dispatch record.","changes_requested",cause="product",action="Print the confirmed new warehouse."),
            {"text":"Returned the incorrect shipment label."},
            tool("delegate_task",assignee="inspector",objective="Continue the outstanding work."),
            write(PATH,"Shipment destination: new warehouse. Verified dispatch record."), {"text":"Fixed the destination."},
            tool("delegate_task",assignee="builder",objective="Verify the revised candidate."),
            tool("read_file",path=PATH),
            submit("Shipment destination: old warehouse. Verified dispatch record.","passed"),
            submit("Shipment destination: new warehouse. Verified dispatch record.","passed"), {"text":"Independently checked the new label."},
            {"text":"Delivered the reviewed label."}]
        detail, provider = self.execute(script)
        self.assertEqual(detail["run"]["status"],"completed",detail["diagnostics"])
        reports=[e["handoff"] for e in detail["room_events"] if e["kind"]=="member_report"]
        self.assertEqual([r["role"] for r in reports],["builder","inspector","builder","inspector"])
        initial, fix, verified = [r["native_review"] for r in reports[1:]]
        self.assertEqual((initial["state"],fix["state"],verified["state"]),("repair","review","passed"))
        self.assertEqual(initial["target"],verified["target"])
        self.assertEqual(initial["candidate_version"],version("Shipment destination: old warehouse. Verified dispatch record."))
        self.assertEqual(verified["candidate_version"],version("Shipment destination: new warehouse. Verified dispatch record."))
        self.assertEqual(verified["assessment"]["results"][0]["receipts"][0]["path"],PATH)
        self.assertTrue(any("候选版本已改变" in str(request[0]) for request in provider.requests))

    def test_identical_test_method_failure_does_not_loop_or_ask_user(self):
        finding = submit("Shipment destination: old warehouse. Verified dispatch record.","inconclusive",cause="verification",action="Obtain independent destination evidence.")
        script = self.start_review()+[finding,{"text":"Method inconclusive."},
            tool("delegate_task",assignee="builder",objective="Continue."),
            tool("read_file",path=PATH),finding,{"text":"Same evidence."},
            {"text":"[BLOCKED] The same evidence cannot verify the destination."}]
        detail, provider = self.execute(script)
        reports=[e["handoff"] for e in detail["room_events"] if e["kind"]=="member_report"]
        self.assertEqual([r["role"] for r in reports],["builder","inspector","inspector"])
        self.assertEqual(reports[-1]["native_review"]["state"],"blocked")
        self.assertNotEqual(detail["run"]["status"],"completed")
        self.assertNotIn("human_question",[e["kind"] for e in detail["room_events"]])

    def test_execution_criterion_needs_a_real_test_and_hard_user_checks_remain_binding(self):
        old = "Shipment destination: old warehouse. Verified dispatch record."
        test = ("from pathlib import Path\nimport unittest\nclass TestLabel(unittest.TestCase):\n"
                "    def test_label(self):\n        self.assertIn('old warehouse',Path('delivery.txt').read_text())\n")
        command = "python3 -m unittest test_label.py"
        version_two = digest({PATH:hashlib.sha256(old.encode()).hexdigest(),
                              "test_label.py":hashlib.sha256(test.encode()).hexdigest()})
        assessment = {"criterion_id":"content", "verdict":"passed", "reason":"Checked the required destination."}
        rule = {**RULE, "method":"execute"}
        script = [tool("delegate_task",assignee="builder", objective="Write a label and test.",deliverable_paths=[PATH,"test_label.py"],
                       remaining_roles=["inspector"]), write(PATH,old), write("test_label.py",test),
                  {"text":"Wrote both files."}, tool("delegate_task",assignee="inspector",objective="Verify.",
                        remaining_roles=[],review={"candidate":"builder","criteria":[rule]}),
                  tool("read_file",path=PATH),
                  tool("submit_review",candidate_version=version_two,results=[{**assessment,"evidence":["file:"+PATH]}]),
                  tool("run_shell",command=command),
                  tool("submit_review",candidate_version=version_two,results=[{**assessment,
                        "evidence":["pass:"+hashlib.sha256((command+"\0").encode()).hexdigest()]}]),
                  {"text":"Verified candidate by executing its test."},
                  {"text":"[BLOCKED] The explicit customer rule still rejects this shipment."}]
        provider = RecordingProvider(script)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run=self.service.start(request("team",conversation=roster("lead","builder","inspector"),
                objective="Check shipping against the confirmed label.",limits={"max_steps":90},
                acceptance_checks=[{"kind":"contains","path":PATH,"text":"another destination"}]))
            detail=finished(self.service,run["id"])
        reports=[e["handoff"] for e in detail["room_events"] if e["kind"]=="member_report"]
        self.assertEqual(reports[-1]["native_review"]["state"],"passed")
        self.assertEqual(reports[-1]["native_review"]["assessment"]["results"][0]["receipts"][0]["kind"],"passed_check")
        self.assertTrue(any("符合标准方法的正面证据" in str(r[0]) for r in provider.requests))
        self.assertNotEqual(detail["run"]["status"],"completed")

    def test_business_question_waits_for_authenticated_input_and_does_not_repeat(self):
        rule=Rule.model_validate(RULE)
        item=Review("target","builder","inspector","Deliver.",[rule],{PATH:"before"})
        queue=ReviewQueue()
        queue.items[item.target]=item
        response={"results":[{"criterion_id":"content","verdict":"inconclusive","cause":"business",
                              "action":"Which warehouse is confirmed?", "receipts":[{"fingerprint":"source"}]}]}
        item.apply(response,"[]")
        self.assertEqual(queue.next("[]").state,"awaiting_user")
        self.assertEqual(queue.next('["North warehouse"]').state,"review")
        item.apply(response,'["North warehouse"]')
        self.assertEqual(item.state,"blocked")

    def test_later_public_change_reopens_old_passed_candidate(self):
        queue=ReviewQueue()
        item=Review("target","builder","inspector","Deliver.",[Rule.model_validate(RULE)],{PATH:"old"},state="passed")
        queue.items[item.target]=item
        queue.published({"task_id":"other","role":"builder","deliverable_paths":[PATH]}, {PATH:"new"},
                        [{"path":PATH,"sha256":"new","change":"modified"}])
        self.assertEqual(item.state,"review")
        self.assertEqual(item.files,{PATH:"new"})

    def test_actual_failing_test_can_be_handed_back_for_product_repair(self):
        old = "Shipment destination: old warehouse. Approved logistics document."
        new = "Shipment destination: new warehouse. Approved logistics document."
        test = ("from pathlib import Path\nimport unittest\nclass TestLabel(unittest.TestCase):\n"
                "    def test_label(self):\n        self.assertIn('new warehouse',Path('delivery.txt').read_text())\n")
        cmd = "python3 -m unittest test_label.py"
        scope = hashlib.sha256((cmd+"\0").encode()).hexdigest()

        def verdict(value, passed):
            revision = digest({PATH:hashlib.sha256(value.encode()).hexdigest(),
                               "test_label.py":hashlib.sha256(test.encode()).hexdigest()})
            result = {"criterion_id":"content","verdict":"passed" if passed else "changes_requested",
                      "reason":"Ran the confirmed destination check.","evidence":["pass:"+scope if passed else "fail:"+hashlib.sha256((cmd+"\0test_label (test_label.TestLabel.test_label)").encode()).hexdigest()]}
            if not passed:
                result.update(cause="product",action="Change the destination to new warehouse.")
            return tool("submit_review",candidate_version=revision,results=[result])

        steps=[tool("delegate_task",assignee="builder",objective="Write shipment materials.",remaining_roles=["inspector"],
                    deliverable_paths=[PATH,"test_label.py"]),write(PATH,old),write("test_label.py",test),{"text":"Draft ready."},
               tool("delegate_task",assignee="inspector",objective="Test the label.",remaining_roles=[],
                    review={"candidate":"builder","criteria":[{**RULE,"method":"execute"}]}),
               tool("run_shell",command=cmd),verdict(old,False),{"text":"Returned the failing destination check."},
               tool("delegate_task",assignee="builder",objective="Repair."),write(PATH,new),{"text":"Corrected the label."},
               tool("delegate_task",assignee="inspector",objective="Recheck."),tool("run_shell",command=cmd),
               verdict(new,True),{"text":"The same check now passes."},{"text":"Shipped."}]
        detail,_=self.execute(steps)
        reports=[e["handoff"] for e in detail["room_events"] if e["kind"]=="member_report"]
        self.assertEqual(detail["run"]["status"],"completed",detail["diagnostics"])
        self.assertEqual([r["native_review"]["state"] for r in reports[1:]],["repair","review","passed"])
        finding=reports[1]["native_review"]["assessment"]["results"][0]
        self.assertEqual(finding["receipts"][0]["kind"],"failed_check")
        self.assertTrue(finding["receipts"][0]["tool_call_id"])
        self.assertNotEqual(finding["receipts"][0]["receipt_id"],finding["receipts"][0]["fingerprint"])
