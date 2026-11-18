"""Exercise provisional candidate selection, peer review and guarded promotion through Studio."""
import hashlib
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.crew_progress import MemberProgress, _check_key, _script_inputs, _shell_inputs
from miniagent.studio_reviews import Review, ReviewQueue, ReviewSession, Rule, digest
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_native_review import PATH, RULE
from tests.test_studio_service import finished, request


RECORDS = '{"bookings":[{"id":"reservation-1","room":"A101"}]}\n'
OLD = "Shipment destination: old warehouse. Independently supplied booking records."
NEW = "Shipment destination: new warehouse. Independently supplied booking records."


def manifest(text):
    return {p:hashlib.sha256(value.encode()).hexdigest() for p,value in
            [(PATH,text),("records.json",RECORDS)]}


def submit(text, passed=True):
    item = {"criterion_id":RULE["id"], "verdict":"passed", "reason":"Read the destination.",
            "evidence":["file:" + PATH]}
    if not passed:
        item.update(verdict="changes_requested", cause="product", action="Correct the destination.")
    return tool("submit_review",candidate_version=digest(manifest(text)), results=[item])


def initial():
    return [tool("delegate_task",assignee="builder",objective="Build shipment label.",remaining_roles=["inspector"],
                 deliverable_paths=[PATH]), write(PATH,OLD), {"text":"[BLOCKED] Independent check needed."},
            tool("delegate_task",assignee="inspector",objective="Independently verify.",
                 review={"candidate":"builder","criteria":[RULE]}), tool("read_file",path=PATH)]


class PrivateCandidateTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("private-review")
        self.local = self.root / "local"
        self.local.mkdir()
        (self.local / "records.json").write_text(RECORDS)
        self.service = StudioService(self.root/"runs",Settings(api_key="test-credential"))
        self.addCleanup(cleanup,self.root)
        self.addCleanup(self.service.close)

    def run_steps(self, steps, observer=None, **kwargs):
        class Provider(RecordingProvider):
            def chat(inner, messages, tools=None, temperature=0):
                if observer:
                    observer(inner.cursor)
                return super().chat(messages,tools,temperature)
        provider = Provider(steps)
        with patch("miniagent.studio_service.DeepSeekProvider",return_value=provider):
            run = self.service.start(request("team",conversation=roster("lead","builder","inspector"),
                objective="Develop and independently verify the label using existing booking records.",
                workspace_path=str(self.local),acceptance_files=[PATH],limits={"max_steps":100}, **kwargs))
            detail = finished(self.service,run["id"])
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"]=="member_report"]
        return detail,reports

    def test_wording_edits_with_no_closed_check_are_routed_to_a_frozen_peer_review(self):
        first, second = "10 units confirmed for the warehouse.", "Total: 10 units confirmed for the warehouse."
        product_check = ("from pathlib import Path\nimport unittest\n"
                         "class Count(unittest.TestCase):\n    def test_count(self):\n"
                         "        self.assertIn('10',Path('delivery.txt').read_text())\n")
        old_probe = "assert 10 == 12, 'unconfirmed target'\n"
        disputed = _check_key(_script_inputs(old_probe)[1])
        positive = "python3 -m unittest test_count.py"
        scope = hashlib.sha256((positive + "\0").encode()).hexdigest()
        files = {**manifest(second), "test_count.py":hashlib.sha256(product_check.encode()).hexdigest()}
        issue = {"criterion_id":"independent_recheck","verdict":"passed",
                 "reason":"Independent expected-count computation matches this candidate.","evidence":["pass:"+scope]}
        original_finding = {**issue, "criterion_id":"probe_"+disputed,
                            "reason":"The old probe assumes twelve. The confirmed target is ten and was tested."}
        steps = [tool("delegate_task",assignee="builder",objective="Show the confirmed total of ten.",
                      remaining_roles=["inspector"],deliverable_paths=[PATH]),
                 write(PATH,first),write("test_count.py",product_check),
                 tool("run_python",code=old_probe), {"text":"[BLOCKED] Probe failed."},
                 tool("delegate_task",assignee="builder",objective="Repair the reported discrepancy."),
                 write(PATH,second),tool("run_shell",command=positive),{"text":"[BLOCKED] Old probe remains."},
                 tool("delegate_task",assignee="builder",objective="Rephrase it again.",deliverable_paths=[PATH]),
                 tool("run_shell",command=positive),
                 tool("submit_review",candidate_version=digest(files),results=[issue]),
                 tool("submit_review",candidate_version=digest(files),results=[issue,original_finding]),
                 {"text":"Checked the confirmed quantity."},{"text":"Delivered."}]
        detail,reports=self.run_steps(steps,acceptance_checks=[{"kind":"contains","path":PATH,"text":"10 units"}])
        self.assertEqual(detail["run"]["status"],"completed",reports)
        self.assertEqual([r["role"] for r in reports],["builder","builder","inspector"])
        first_report,second_report,peer=reports
        self.assertNotIn("unresolved_after_repair",first_report["reviewable_candidate"])
        self.assertTrue(second_report["reviewable_candidate"]["unresolved_after_repair"])
        self.assertEqual(second_report["reviewable_candidate"]["candidate_version"],digest(files))
        self.assertEqual(peer["native_review"]["objective"],first_report["objective"])
        self.assertEqual(peer["promoted_candidate"]["source_task"],second_report["task_id"])
        self.assertEqual((self.local/PATH).read_text(),second)

    def test_historical_probe_needs_a_current_assertion_beyond_a_process_exit(self):
        progress = MemberProgress(self.local)
        files = progress.project_versions()
        criterion = Rule(id="probe_old", description="Independently check this observation.", method="execute")
        review = Review("candidate","author","inspector","Check total.",[criterion],files,probes={"old": {}})
        session = ReviewSession(review,files,progress,"independent-attempt")
        session._add("pass:exit", {"kind":"passed_check","asserted":False}, "process-call")
        result = session.submit(None,{"candidate_version":review.version,"results":[{
            "criterion_id":criterion.id,"verdict":"passed","reason":"The process exited successfully.",
            "evidence":["pass:exit"]}]})
        self.assertFalse(result.ok)
        self.assertIn("断言",result.content)
        self.assertIsNone(session.assessment)

    def test_aggregate_191_9_failure_can_be_adjudicated_after_repair_by_an_independent_peer(self):
        # Same incident shape: a rewritten inherited verifier prints only a
        # tally; its corrected 200/0 is not a waiver of the original failure.
        (self.local / "verify.py").write_text("# inherited checker, to be updated for this delivery\n")
        label = "10 units confirmed for the warehouse.\n"
        old = '''from pathlib import Path
import sys
text = Path("delivery.txt").read_text()
PASS, FAIL = [], []
def chk(condition, caption):
    (PASS if condition else FAIL).append(caption)
for i in range(191):
    chk("10 units" in text, "quantity")
for i in range(9):
    chk(text.count("\\n") + 1 == 1, "line count")
print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    print("\\n".join(FAIL))
    sys.exit(1)
'''
        fixed = old.replace('text.count("\\n") + 1', 'len(text.splitlines())')
        cmd = 'python3 verify.py; echo "EXIT=$?"'
        disputed = _check_key(_shell_inputs(cmd, ".", {})[1])
        independent = ('from pathlib import Path\ntext = Path("delivery.txt").read_text()\n'
                       'assert len(text.splitlines()) == 1\nassert "10 units" in text\n'
                       'print("PASS independent quantity and line count")\n')
        proof = "pass:" + _check_key(_script_inputs(independent)[1])
        files = {**manifest(label), "verify.py":hashlib.sha256(fixed.encode()).hexdigest()}
        finding = {"criterion_id":"independent_recheck", "verdict":"passed", "evidence":[proof],
                   "reason":"Read actual delivery. Independently counted one terminated line and confirmed ten units."}
        prior = {**finding, "criterion_id":"probe_" + disputed,
                 "reason":"All nine old failures repeat an off-by-one line count. The original artifact has one trailing newline; splitlines and the independent quantity assertion pass."}
        steps = [tool("delegate_task",assignee="builder",objective="Confirm ten units and update the inherited checker.",
                      remaining_roles=["inspector"],deliverable_paths=[PATH]),
                 write(PATH,label),write("verify.py",old),tool("run_shell",command=cmd),
                 {"text":"[BLOCKED] Original checker failed."},
                 tool("delegate_task",assignee="builder",objective="Correct the line-count calculation and preserve the original failure evidence."),
                 write("verify.py",fixed),tool("run_shell",command=cmd),
                 {"text":"[BLOCKED] Corrected checker reports 200/0; the original failure needs independent review."},
                 tool("delegate_task",assignee="builder",objective="Resolve remaining checker dispute.",deliverable_paths=[PATH]),
                 tool("run_python",code=independent),
                 tool("submit_review",candidate_version=digest(files),results=[finding,prior]),
                 {"text":"Independently checked the original discrepancy."},{"text":"Delivered."}]
        captured = []
        original = ReviewQueue.open
        def capture(queue, *args, **kwargs):
            result = original(queue, *args, **kwargs)
            captured.append(result)
            return result
        with patch.object(ReviewQueue,"open",capture):
            detail,reports = self.run_steps(steps,acceptance_checks=[{"kind":"contains","path":PATH,"text":"10 units"}])
        self.assertEqual(detail["run"]["status"],"completed",reports)
        self.assertEqual([r["role"] for r in reports],["builder","builder","inspector"])
        self.assertTrue(reports[0]["evidence"]["observed_failure"])
        self.assertTrue(reports[1]["evidence"]["observed_failure"])
        failure, = reports[0]["evidence"]["failed_check_details"]
        self.assertEqual((failure["tool_exit_code"],failure["check_exit_code"]),(0,1))
        probe = captured[-1].probes[disputed]
        self.assertEqual(probe["kind"],"edited_script")
        self.assertEqual(probe["code"],old)
        self.assertEqual(probe["sha256"],hashlib.sha256(old.encode()).hexdigest())
        self.assertIn("PASS 191 / FAIL 9",probe["error"])
        self.assertEqual(probe["error"].count("line count"),9)
        self.assertEqual(reports[-1]["native_review"]["state"],"passed")
        self.assertEqual((self.local/PATH).read_text(),label)

    def test_author_can_close_a_genuine_regression_and_publish_without_review_reroute(self):
        fixed = "Shipment to the new warehouse, confirmed by operations."
        suite = ("from pathlib import Path\ntry:\n    assert 'new warehouse' in Path('delivery.txt').read_text()\n"
                 "except AssertionError:\n    print('FAIL destination')\n    raise\nprint('PASS destination')\n")
        run = tool("run_shell",command="python3 test_address.py")
        steps = [tool("delegate_task",assignee="builder",objective="Update the destination.",
                      remaining_roles=[],deliverable_paths=[PATH]),write(PATH,OLD),write("test_address.py",suite),
                 run,{"text":"[BLOCKED] The destination is incorrect."},
                 tool("delegate_task",assignee="builder",objective="Fix the address and rerun the same test."),
                 write(PATH,fixed),run,{"text":"The original test passed."},{"text":"Delivered."}]
        detail,reports = self.run_steps(steps,acceptance_checks=[{"kind":"contains","path":PATH,"text":"new warehouse"}])
        self.assertEqual(detail["run"]["status"],"completed",reports)
        self.assertEqual([r["role"] for r in reports],["builder","builder"])
        self.assertTrue(reports[-1]["published"])
        self.assertFalse(reports[-1]["evidence"]["observed_failure"])
        self.assertFalse(any(e["kind"]=="runtime_reroute" for e in detail["room_events"]))

    def test_pass_promotes_original_under_author_identity_with_records(self):
        detail,reports = self.run_steps(initial()+[submit(OLD),{"text":"Independently verified."},{"text":"Delivered."}])
        self.assertEqual(detail["run"]["status"],"completed",reports)
        first,last = reports
        self.assertFalse(first["published"])
        self.assertTrue(last["published"])
        self.assertEqual(first["reviewable_candidate"]["candidate_version"],digest(manifest(OLD)))
        self.assertEqual(last["promoted_candidate"],{"author":"builder","source_task":first["task_id"],
            "candidate_version":digest(manifest(OLD)),"reviewer":"inspector","review_task":last["task_id"]})
        self.assertEqual((self.local/PATH).read_text(),OLD)
        self.assertEqual((self.local/"records.json").read_text(),RECORDS)

    def test_reject_then_private_repair_needs_new_independent_review(self):
        projection = []
        def observe(index):
            if index in {3,7,10}:
                matches = list((self.root/"runs").glob("*/agents/lead"))
                projection.append([(p/PATH).exists() for p in matches])
        steps = initial()+[submit(OLD,False),{"text":"Wrong destination."},
                tool("delegate_task",assignee="builder",objective="Correct it."),
                write(PATH,NEW),{"text":"Corrected."},
                tool("delegate_task",assignee="inspector",objective="Recheck the revision."),
                tool("read_file",path=PATH),submit(NEW),{"text":"Verified revision."},{"text":"Delivered."}]
        detail,reports = self.run_steps(steps,observe)
        self.assertEqual(detail["run"]["status"],"completed",reports)
        self.assertEqual(projection,[[False],[False],[False]])
        self.assertEqual([r["published"] for r in reports],[False,False,False,True])
        self.assertEqual([r["native_review"]["state"] for r in reports[1:]],["repair","review","passed"])
        self.assertEqual(reports[-1]["promoted_candidate"]["source_task"],reports[2]["task_id"])
        self.assertEqual(reports[-1]["native_review"]["target"],reports[0]["task_id"])
        self.assertEqual((self.local/PATH).read_text(),NEW)

    def test_named_report_is_separate_from_old_report_and_candidate_version(self):
        prior = "Previous room QA and existing reservation audit."
        (self.local/"qa_report.md").write_text(prior)
        files = {**manifest(OLD),"qa_report.md":hashlib.sha256(prior.encode()).hexdigest()}
        name = "review-notes/qa_report.md"
        steps = initial()
        steps[3]["tool_calls"][0]["arguments"]["deliverable_paths"] = ["qa_report.md"]
        steps[3]["tool_calls"][0]["arguments"]["review"]["criteria"].append({**RULE,"id":"qa-report"})
        assessment=submit(OLD)
        args=assessment["tool_calls"][0]["arguments"]
        args["candidate_version"]=digest(files)
        args["results"].append({"criterion_id":"qa-report","verdict":"passed","reason":"Recorded findings.",
                                "evidence":["note:" + name]})
        steps += [write(name,"Current independent checker findings."),tool("read_file",path=name),assessment,
                  {"text":"Verified candidate and attached findings."},{"text":"Delivered."}]
        detail,reports=self.run_steps(steps)
        self.assertEqual(detail["run"]["status"],"completed",reports)
        self.assertEqual(reports[-1]["promoted_candidate"]["candidate_version"],digest(files))
        self.assertEqual(reports[-1]["review_documents"],{"qa_report.md":name})
        self.assertIn("Current independent",reports[-1]["diagnostics"][0]["excerpt"])
        self.assertEqual((self.local/"qa_report.md").read_text(),prior)
        self.assertFalse((self.local/name).exists())

    def test_known_failed_check_cannot_be_overridden_by_reading_a_file(self):
        steps=initial()
        steps[2:2]=[write("test_label.py","import unittest\nclass Label(unittest.TestCase):\n"
                            "    def test_failure(self): self.assertEqual(1, 2)\n"),
                     tool("run_shell",command="python3 -m unittest test_label.py")]
        assertion=submit(OLD)
        code=steps[2]["tool_calls"][0]["arguments"]["content"]
        assertion["tool_calls"][0]["arguments"]["candidate_version"]=digest({
            **manifest(OLD),"test_label.py":hashlib.sha256(code.encode()).hexdigest()})
        steps += [assertion,{"text":"[BLOCKED] Failed test still applies."},{"text":"[BLOCKED] Unreleased."}]
        _,reports=self.run_steps(steps)
        self.assertTrue(reports[0]["evidence"]["observed_failure"])
        self.assertIsNone(reports[-1]["native_review"]["assessment"])
        self.assertFalse(reports[-1]["published"])
        self.assertFalse((self.local/PATH).exists())

    def test_user_check_rejects_even_after_peer_assessment(self):
        detail,reports = self.run_steps(initial()+[submit(OLD),{"text":"Verified."},{"text":"[BLOCKED] Hard rule failed."}],
            acceptance_checks=[{"kind":"contains","path":PATH,"text":"new warehouse"}])
        self.assertNotEqual(detail["run"]["status"],"completed")
        self.assertEqual(reports[-1]["native_review"]["assessment"]["verdict"],"passed")
        self.assertEqual(reports[-1]["native_review"]["state"],"repair")
        self.assertIn("用户指定验收失败",reports[-1]["native_review"]["host_issue"])
        self.assertIn("用户指定验收失败",reports[-1]["remaining_issue"])
        self.assertFalse((self.local/PATH).exists())

    def test_reviewer_addition_is_not_part_of_approved_candidate(self):
        _,reports=self.run_steps(initial()+[write("notes.md","Unexpected file in candidate."),
            submit(OLD),{"text":"[BLOCKED] Tree changed."},{"text":"[BLOCKED] No reviewed version."}])
        self.assertFalse(reports[-1]["published"])
        self.assertIsNone(reports[-1]["native_review"]["assessment"])
        self.assertFalse((self.local/"notes.md").exists())
        self.assertFalse((self.local/PATH).exists())

    def test_concurrent_public_edit_and_checkpoint_tampering_are_rejected(self):
        for tamper in ("base","checkpoint"):
            with self.subTest(tamper=tamper):
                previous = set((self.root/"runs").glob("*/agents/lead"))
                def mutate(index):
                    if index==6:
                        pattern="*/agents/lead/records.json" if tamper=="base" else "*/assignments/*/checkpoint/agents/builder/delivery.txt"
                        path = next(p for p in (self.root/"runs").glob(pattern)
                                    if next(d for d in p.parents if d.parent==self.root/"runs")/"agents/lead" not in previous)
                        path.write_text("Concurrent edit preserved.")
                _,reports=self.run_steps(initial()+[submit(OLD),{"text":"Verified."},{"text":"[BLOCKED] No current version."}],mutate)
                self.assertFalse(reports[-1]["published"],reports)
                self.assertEqual(reports[-1]["native_review"]["state"],"blocked")
                self.assertFalse((self.local/PATH).exists())
                if tamper=="base":
                    self.assertEqual(next((self.root/"runs").glob("*/agents/lead/records.json")).read_text(),"Concurrent edit preserved.")

    def test_selection_requires_same_room_task_current_revision_and_complete_outputs(self):
        queue=ReviewQueue()
        row={"task_id":"child","role":"builder","published":False,"objective":"Build."}
        draft={"task_id":"child","version":2,"reviewable":True,"manifest":manifest(OLD)}
        for candidate,reviewer,version,valid in [
            ("other-room-child","inspector",2,True),("child","builder",2,True),
            ("child","inspector",1,True),("child","inspector",2,False)]:
            with self.subTest(candidate=candidate,reviewer=reviewer,version=version,valid=valid):
                with self.assertRaises(ValueError):
                    queue.open({"candidate":candidate,"criteria":[RULE]},reviewer,[row],
                               drafts={"builder":{**draft,"reviewable":valid}},project_version=version)
