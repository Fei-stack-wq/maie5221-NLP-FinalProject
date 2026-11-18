import hashlib
import json
import unittest
from unittest.mock import Mock, patch

from miniagent.config import Settings
from miniagent.research import Page
from miniagent.studio_reviews import Review, ReviewQueue, ReviewSession, Rule, digest
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

    def execute(self, steps, *others):
        provider = RecordingProvider(steps)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.service.start(request("team",conversation=roster("lead","builder","inspector",*others),
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

    def reviewed_label(self):
        label = "Shipment destination: old warehouse. Verified dispatch record."
        script = self.start_review()
        # A real stale delegation error preceded the accepted review.
        script.insert(3, tool("delegate_task", assignee="inspector", objective="Verify the label.",
                              remaining_roles=[], review={"candidate": "unknown-target", "criteria": [RULE]}))
        return script + [submit(label, "passed"), {"text": "Independent review published."}]

    def close_events(self, detail):
        trace = self.root / "runs" / detail["run"]["id"] / "trace.jsonl"
        return [row for line in trace.read_text().splitlines()
                if (row := json.loads(line))["kind"] == "reviewed_delivery_blocked"]

    def test_confirmed_receipt_supersedes_prior_denial_at_coordinator_close(self):
        script = self.reviewed_label() + [{"text": "[BLOCKED] Independent verification never started."},
                                          {"text": "Delivered the independently verified label."}]
        detail, provider = self.execute(script)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        events = self.close_events(detail)
        self.assertEqual([e["action"] for e in events], ["request_clarification"])
        receipt = events[0]["receipts"][0]
        self.assertEqual(receipt["state"], "passed")
        self.assertEqual(receipt["reviewer"], "inspector")
        self.assertEqual(receipt["required_criteria"], ["content"])
        self.assertEqual(receipt["candidate_version"], digest(receipt["files"]))
        prompts = [str(messages) for messages, _ in provider.requests[-2:]]
        self.assertTrue(all(receipt["files"][PATH] in msg for msg in prompts))
        self.assertTrue(all("[delegate_task 未完成]" not in msg for msg in prompts))
        self.assertIn("宿主结案核对", prompts[-1])

    def test_review_of_one_artifact_cannot_confirm_an_unreviewed_required_file(self):
        script = self.reviewed_label()
        # A distinct deliverable exists (so the file gate passes) but was
        # authored by the reviewer and is outside the reviewed candidate.
        script.insert(-1, write("certificate.txt", "This dispatch certificate requires its own approval."))
        script.append({"text": "[BLOCKED] The second deliverable has no independent approval."})
        provider = RecordingProvider(script)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.service.start(request("team", conversation=roster("lead", "builder", "inspector"),
                                     objective="Validate both shipping documents.",
                                     acceptance_files=[PATH, "certificate.txt"], limits={"max_steps": 90}))
            detail = finished(self.service, run["id"])
        self.assertEqual(detail["run"]["status"], "failed")
        published = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual(published[-1]["native_review"]["state"], "passed")
        self.assertEqual(self.close_events(detail), [])

    def test_verified_scope_cannot_override_a_separate_publication_blocker(self):
        blocker = {"text": "[BLOCKED] The shipment needs a carrier permit outside the label review."}
        detail, provider = self.execute(self.reviewed_label() + [blocker, blocker])
        self.assertEqual(detail["run"]["status"], "failed")
        self.assertEqual([e["action"] for e in self.close_events(detail)],
                         ["request_clarification", "preserve_block"])
        self.assertEqual(len([1 for messages, _ in provider.requests if "宿主结案核对" in str(messages)]), 1)

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

    def test_source_review_requires_both_candidate_and_independently_fetched_source(self):
        article = "Shipment destination: old warehouse. Verified dispatch record."
        url = "https://example.org/dispatch"
        review = self.start_review()
        criterion = {**RULE, "method": "source"}
        review[3] = tool("delegate_task", assignee="inspector", objective="Verify the factual claim.", remaining_roles=[],
                         review={"candidate": "builder", "criteria": [criterion]})
        claimed = submit(article, "passed")
        verified = submit(article, "passed")
        verified["tool_calls"][0]["arguments"]["results"][0]["evidence"].append("source:" + url)
        steps = review + [claimed, tool("http_get", url=url), verified, {"text": "Verified against the dispatch record."},
                          {"text": "Delivered."}]
        with patch("miniagent.research.fetch", return_value=Page(url, "text/plain", article.encode(), False)):
            detail, provider = self.execute(steps)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertTrue(any("本次亲自打开的来源" in str(request[0]) for request in provider.requests))
        report = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"][-1]
        receipts = report["native_review"]["assessment"]["results"][0]["receipts"]
        evidence = next(r for r in receipts if r["kind"] == "source")
        self.assertEqual(evidence["url"], url)
        self.assertEqual(evidence["observed_bytes_sha256"], hashlib.sha256(article.encode()).hexdigest())

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

    def verification_retry(self, retry_steps):
        label = "Shipment destination: old warehouse. Verified dispatch record."
        url = "https://issuer.example/dispatch.pdf"
        rules = [RULE, {"id": "issuer_notice", "method": "source", "description": "Check the issuer's dispatch notice."}]
        checked = {"criterion_id": "content", "verdict": "passed", "reason": "Read the label.", "evidence": ["file:" + PATH]}
        gap = {"criterion_id": "issuer_notice", "verdict": "inconclusive", "cause": "verification",
               "action": "Locate a readable issuer copy.", "reason": "The PDF returns HTTP 403.", "evidence": ["file:" + PATH]}
        steps = self.start_review()
        steps[3] = tool("delegate_task", assignee="inspector", objective="Review both criteria.", remaining_roles=[],
                        review={"candidate": "builder", "criteria": rules})
        steps += [tool("http_get", url=url), tool("submit_review", candidate_version=version(label), results=[checked, gap]),
                  {"text": "The PDF returned HTTP 403."},
                  tool("delegate_task", assignee="backup", objective="Try the issuer's accessible alternate notice.",
                       deliverable_paths=["backup-review.txt"], review={"candidate": "builder", "criteria": [{**RULE, "description": "Reading the label suffices."}]}),
                  tool("read_file", path=PATH)]
        return steps + retry_steps(label, gap), rules

    def test_resume_bound_rule_with_alternate_source_and_retain_verified_receipts(self):
        alternate = "https://issuer.example/dispatch.html"
        def finish(label, gap):
            positive = {"criterion_id": gap["criterion_id"], "verdict": "passed", "evidence": ["file:" + PATH],
                        "reason": "Issuer HTML confirms the named warehouse."}
            return [tool("submit_review", candidate_version=version(label), results=[positive]),
                    tool("http_get", url=alternate),
                    tool("submit_review", candidate_version=version(label),
                         results=[{**positive, "evidence": ["file:" + PATH, "source:" + alternate]}]),
                    {"text": "Verified the remaining source."}, {"text": "Delivered."}]
        steps, rules = self.verification_retry(finish)
        with patch("miniagent.research.fetch", side_effect=[OSError("HTTP 403"), Page(alternate,"text/plain",b"Official destination.",False)]):
            detail, provider = self.execute(steps, "backup")
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        reports = [e["handoff"]["native_review"] for e in detail["room_events"]
                   if e["kind"] == "member_report" and "native_review" in e["handoff"]]
        self.assertEqual([r["state"] for r in reports], ["review", "passed"])
        first, second = reports
        self.assertEqual(first["candidate_version"], second["candidate_version"])
        self.assertEqual(first["reviewer"], second["reviewer"])
        self.assertEqual(first["criteria"], second["criteria"])
        self.assertEqual(first["findings"][0], second["findings"][0])  # preserves the previous evaluator and attempt
        self.assertEqual(second["findings"][1]["receipts"][1]["url"], alternate)
        resume, = [e for e in detail["room_events"] if e["kind"] == "review_resumed"]
        self.assertEqual((resume["requested"], resume["recipient"]), ("backup", "inspector"))
        self.assertEqual(resume["pending_criteria"], ["issuer_notice"])
        self.assertEqual([r["id"] for r in resume["bound_criteria"]], [r["id"] for r in rules])
        self.assertTrue(any("本次亲自打开的来源" in str(req[0]) for req in provider.requests))
        self.assertTrue(any("accessible alternate" in str(req[0]) and "Locate a readable issuer copy" in str(req[0])
                            for req in provider.requests))

    def test_unresolved_403_cannot_purchase_further_verification_by_changing_read_ranges(self):
        def finish(label, gap):
            return [tool("http_get", url="https://issuer.example/dispatch.pdf"), tool("read_file", path=PATH, offset=9, limit=23),
                    tool("submit_review", candidate_version=version(label), results=[gap]),
                    {"text": "Independent source remains inaccessible."}, {"text": "[BLOCKED] Notice inaccessible."}]
        steps, _ = self.verification_retry(finish)
        with patch("miniagent.research.fetch", side_effect=OSError("HTTP 403")):
            detail, _ = self.execute(steps, "backup")
        self.assertEqual(detail["run"]["status"], "failed")
        reviews = [e["handoff"]["native_review"] for e in detail["room_events"]
                   if e["kind"] == "member_report" and "native_review" in e["handoff"]]
        self.assertEqual([r["state"] for r in reviews], ["review", "blocked"])
        self.assertNotEqual(reviews[0]["findings"][1]["receipts"][0]["fingerprint"],
                            reviews[1]["findings"][1]["receipts"][0]["fingerprint"])
        self.assertTrue(all(r["findings"][1]["verdict"] == "inconclusive" for r in reviews))
        self.assertNotIn("human_question", [e["kind"] for e in detail["room_events"]])

    def test_method_retry_preserves_only_the_same_version_and_intact_source_evidence(self):
        item = Review("target", "writer", "inspector", "Verify the shipping notice.", [
            Rule.model_validate(RULE), Rule(id="other", description="Check the issuer.", method="source")
        ], {PATH: "v1"})
        retained = {"criterion_id": "content", "verdict": "passed", "receipts": [
            {"kind": "read", "path": PATH, "sha256": "v1"}, {"kind": "source", "path": "source.json", "sha256": "s1"}]}
        other = {"criterion_id": "other", "verdict": "inconclusive", "cause": "verification", "receipts": []}
        item.latest = {"candidate_version": item.version, "results": [retained, other]}
        manifest = {PATH: "v1", "source.json": "s1"}
        progress = Mock()
        progress.project_versions.return_value = manifest
        session = ReviewSession(item, manifest.copy(), progress, "retry")
        self.assertEqual(session.retained_findings(), {"content": retained})
        del manifest["source.json"]
        self.assertEqual(session.retained_findings(), {})
        manifest["source.json"] = "s1"
        item.files[PATH] = manifest[PATH] = "v2"
        self.assertEqual(session.retained_findings(), {})

    def test_method_retry_can_continue_only_when_required_gap_set_shrinks(self):
        rules = [Rule(id=name, description="Verify independently.", method="inspect") for name in ["a", "b"]]
        item = Review("target", "writer", "inspector", "Review.", rules, {PATH: "v1"})
        def finding(rule, okay, evidence):
            return {"criterion_id": rule, "verdict": "passed" if okay else "inconclusive",
                    "cause": None if okay else "verification", "receipts": [{"fingerprint": evidence}]}
        def apply(a_ok, tag):
            item.apply({"candidate_version": item.version, "results": [finding("a", a_ok, tag), finding("b", False, tag)]}, "[]")
            return item.state
        self.assertEqual(apply(False, "first"), "review")
        self.assertEqual(apply(True, "second"), "review")
        self.assertEqual(apply(True, "different-paging"), "blocked")

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
        self.assertEqual(self.close_events(detail), [])

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
