"""Exercise provisional candidate selection, peer review and guarded promotion through Studio."""
import ast
import hashlib
import json
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.crew_progress import MemberProgress, _check_key, _script_inputs, _shell_inputs, _shell_case_suite
from miniagent.studio_crew import RoomCrew
from miniagent.studio_reviews import Review, ReviewQueue, ReviewSession, Rule, digest
from miniagent.schemas import ToolResult
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

    def test_corrected_selector_on_first_handoff_requires_independent_frozen_product_assertion(self):
        html = '<label for="startSelect">Start</label><select id="startSelect"><option>09:00</option></select>'
        page = "from pathlib import Path\npage.goto(Path('booking.html').resolve().as_uri())\n"
        wrong = page + "assert page.locator('#startInput').count() == 1"
        corrected = page + "assert page.locator('#startSelect').count() == 1"
        peer_code = page + "assert page.get_by_label('Start').input_value() == '09:00'"
        def browser_scope(code):
            return _check_key("browser " + hashlib.sha256((".\0" + ast.dump(ast.parse(code))).encode()).hexdigest())
        bad = browser_scope(wrong)
        unrelated = "from pathlib import Path\nassert 'A101' in Path('records.json').read_text()\nprint('PASS unrelated room')"
        version = digest({**manifest(NEW), "booking.html": hashlib.sha256(html.encode()).hexdigest()})

        def assessment(check, version=version):
            def finding(key):
                return {"criterion_id":key, "verdict":"passed", "reason":"The accessible booking selector has its expected value.",
                        "evidence":[check]}
            return tool("submit_review",candidate_version=version,
                        results=[finding("independent_recheck"),finding("probe_" + bad)])

        steps = [tool("delegate_task", assignee="builder", objective="Deliver a working meeting-room time selector.",
                      remaining_roles=["inspector"], deliverable_paths=[PATH, "booking.html"]),
                 write(PATH,NEW), write("booking.html",html),
                 tool("run_browser",code=wrong), tool("run_browser",code=corrected),
                 {"text":"Booking is complete; the first script used a guessed selector."},
                 tool("delegate_task",assignee="inspector",objective="Independently verify booking."),
                 tool("run_python",code=unrelated), assessment("pass:" + _check_key(_script_inputs(unrelated)[1])),
                 tool("run_browser",code=peer_code), assessment("pass:" + browser_scope(peer_code),"0"*64),
                 assessment("pass:" + browser_scope(peer_code)),
                 {"text":"Independent selector verification passed."},{"text":"Delivered."}]
        def browser(ctx, args):
            # A deterministic tool double; the normal host still records the
            # script, exit, candidate path, call, actor, and SHA itself.
            self.assertEqual((ctx.sandbox.root / "booking.html").read_text(), html)
            observed = {"booking.html":hashlib.sha256(html.encode()).hexdigest()}
            if args["code"] == wrong:
                return ToolResult.failure("exit_code=1\nstderr:\nAssertionError: #startInput missing", exit_code=1,
                                          asserted_browser_files=observed)
            self.assertIn(args["code"], (corrected, peer_code))
            return ToolResult.success("exit_code=0\nstdout:\nPASS selector",exit_code=0,asserted_browser_files=observed)
        with patch("miniagent.tools.run_browser", side_effect=browser):
            detail, reports = self.run_steps(steps)
        self.assertEqual(detail["run"]["status"], "completed", reports)
        author, independent = reports
        self.assertFalse(author["published"])
        self.assertTrue(author["evidence"]["observed_failure"])
        self.assertEqual(author["author_probe_review"]["candidate_version"], version)
        self.assertEqual(set(author["author_probe_review"]["original_calls"]), {bad})
        self.assertEqual(independent["role"], "inspector")
        self.assertEqual(independent["promoted_candidate"]["source_task"], author["task_id"])
        self.assertEqual(independent["native_review"]["state"], "passed")
        self.assertEqual((self.local / "booking.html").read_text(), html)
        trace_file = self.root / "runs" / detail["run"]["id"] / "trace.jsonl"
        events = [json.loads(line) for line in trace_file.read_text().splitlines()]
        submits = [e["ok"] for e in events if e["kind"]=="tool_result" and e.get("tool")=="submit_review"]
        self.assertEqual(submits, [False,False,True])
        self.assertTrue(any(e["kind"]=="handoff_pending_peer" for e in events))

    def test_repairing_published_candidate_retains_original_business_review_and_old_failure(self):
        from tests.test_studio_native_review import submit as submit_public
        read = "from pathlib import Path\nlabel = Path('delivery.txt').read_text()\n"
        wrong = read + "assert 'Old-id: Start' in label"
        corrected = read + "assert 'new warehouse' in label\nprint('PASS corrected destination')"
        verified = read + "assert label.split('.')[0].endswith('new warehouse')\nprint('PASS peer destination')"
        old_scope = _check_key(_script_inputs(wrong)[1])
        own_scope = _check_key(_script_inputs(corrected)[1])
        peer_scope = _check_key(_script_inputs(verified)[1])
        findings = [{"criterion_id":"content","verdict":"passed","evidence":["file:" + PATH],"reason":"New destination confirmed."},
                    {"criterion_id":"probe_" + old_scope,"verdict":"passed","evidence":["pass:" + peer_scope],
                     "reason":"The old test selected an unrelated identifier; the actual destination matches."}]
        steps = [tool("delegate_task",assignee="builder",objective="Build the label.",remaining_roles=["inspector"],
                      deliverable_paths=[PATH]), write(PATH,OLD), {"text":"First edition delivered."},
                 tool("delegate_task",assignee="inspector",objective="Inspect the label.", review={"candidate":"builder","criteria":[RULE]}),
                 tool("read_lines",path=PATH),submit_public(OLD,"changes_requested",cause="product",action="Use the new warehouse."),
                 {"text":"Old location must be corrected."},
                 tool("delegate_task",assignee="builder",objective="Fix the destination."),
                 write(PATH,NEW), tool("run_python",code=wrong), tool("run_python",code=corrected),
                 {"text":"Repaired the label; my original check used the wrong selector."},
                 tool("delegate_task",assignee="inspector",objective="Verify the new candidate."),tool("read_lines",path=PATH),
                 tool("submit_review",candidate_version=digest(manifest(NEW)),results=[
                     findings[0],{**findings[1],"evidence":["pass:"+own_scope]}]),
                 tool("run_python",code=verified),tool("submit_review",candidate_version=digest(manifest(NEW)),results=findings),
                 {"text":"Verified the product."},{"text":"Delivered."}]
        detail, reports = self.run_steps(steps)
        self.assertEqual(detail["run"]["status"],"completed",reports)
        self.assertEqual([r["role"] for r in reports],["builder","inspector","builder","inspector"])
        first, first_review, repaired, signed = reports
        self.assertEqual(first_review["native_review"]["state"],"repair")
        self.assertEqual(repaired["author_probe_review"]["target"],first["task_id"])
        self.assertFalse(repaired["published"])
        self.assertEqual([c["id"] for c in signed["native_review"]["criteria"]],["content","probe_"+old_scope])
        self.assertEqual(signed["promoted_candidate"]["source_task"],repaired["task_id"])
        self.assertEqual(signed["native_review"]["assessment"]["evaluator"],"inspector")
        self.assertEqual((self.local / PATH).read_text(),NEW)

    def test_qa_without_deliverables_needs_peer_other_than_product_publisher(self):
        read = "from pathlib import Path\nvalue = Path('delivery.txt').read_text()\n"
        wrong = read + "assert 'missing label' in value"
        fixed = read + "assert 'new warehouse' in value\nprint('PASS actual location')"
        peer = read + "assert value.startswith('Shipment destination: new warehouse.')\nprint('PASS frozen location')"
        old = _check_key(_script_inputs(wrong)[1])
        current = _check_key(_script_inputs(peer)[1])
        steps = [tool("delegate_task",assignee="builder",objective="Write label.",remaining_roles=["inspector","lead"],
                      deliverable_paths=[PATH]),write(PATH,NEW),{"text":"Released."},
                 tool("delegate_task",assignee="inspector",objective="Test the existing label."),
                 tool("run_python",code=wrong),tool("run_python",code=fixed),{"text":"The corrected location check passed."},
                 tool("delegate_task",assignee="builder",objective="Check QA's correction."),
                 tool("run_python",code=peer),
                 tool("submit_review",candidate_version=digest(manifest(NEW)),results=[
                     {"criterion_id":key,"verdict":"passed","evidence":["pass:"+current],
                      "reason":"On the frozen label the original test asked for unrelated text."}
                     for key in ("independent_recheck","probe_"+old)]),
                 {"text":"Independently verified the label."},{"text":"Finished."}]
        detail,reports = self.run_steps(steps)
        self.assertEqual(detail["run"]["status"],"completed",reports)
        author,qa,arbiter=reports
        self.assertEqual(qa["deliverable_paths"],[])
        self.assertEqual(qa["changed_files"],[])
        self.assertEqual(qa["author_probe_review"]["reviewer"],"lead")
        self.assertNotIn(arbiter["role"],[author["role"],qa["role"]])
        self.assertEqual(arbiter["native_review"]["state"],"passed")
        self.assertEqual(arbiter["changed_files"],[])

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

    def test_named_snapshot_check_can_be_independently_adjudicated_without_editing_it(self):
        label = "10 units confirmed for the warehouse."
        source = '{"retrieved_at":"2026-11-18T16:53:23+00:00","url":"https://example.org/source"}\n'
        original = r'''import json, re, sys
from pathlib import Path
snapshot = json.loads(Path("sources/entry.json").read_text())
# This check incorrectly rejects an ISO timestamp because T is a word character.
ok = bool(re.search(r"\b20\d{2}-\d{2}-\d{2}\b", json.dumps(snapshot)))
print("PASS snapshot date" if ok else "FAIL snapshot date")
sys.exit(0 if ok else 1)
'''
        command = "python3 verify.py"
        source_sha = hashlib.sha256(original.encode()).hexdigest()
        case_suite = _shell_case_suite(command, ".") + "\0sha256=" + source_sha
        disputed = _check_key(case_suite, "snapshot date")
        independent = ('import json\nfrom datetime import datetime, timezone\nfrom pathlib import Path\n'
                       's = json.loads(Path("sources/entry.json").read_text())\n'
                       'assert s["url"].startswith("https://")\n'
                       'assert datetime.fromisoformat(s["retrieved_at"]) <= datetime(2026,11,19,tzinfo=timezone.utc)\n'
                       'assert "10 units" in Path("delivery.txt").read_text()\n'
                       'print("PASS independent source and delivery")\n')
        proof = "pass:" + _check_key(_script_inputs(independent)[1])
        files = {**manifest(label), "verify.py":source_sha,
                 "sources/entry.json":hashlib.sha256(source.encode()).hexdigest()}
        finding = {"criterion_id":"independent_recheck", "verdict":"passed", "evidence":[proof],
                   "reason":"Checked the actual label and parsed the retained source timestamp."}
        prior = {**finding, "criterion_id":"probe_" + disputed,
                 "reason":"The frozen named assertion rejects an ISO date followed by T. Independent parsing verifies the actual retrieval date without changing the source or original assertion."}
        browser_code = "assert not js_errors\n" + "# browser setup diagnostic\n" * 250
        browser_scope = _check_key("browser " + hashlib.sha256(
            (".\0" + ast.dump(ast.parse(browser_code))).encode()).hexdigest())
        browser_finding = {**finding, "criterion_id":"probe_" + browser_scope,
                           "reason":"The original browser probe refers to an undefined harness variable. Independently checked the original requested delivery and source traceability; a harness variable is not a product requirement."}
        steps = [tool("delegate_task",assignee="builder",objective="Deliver ten units and retain traceable source metadata.",
                      remaining_roles=["inspector"],deliverable_paths=[PATH]),
                 write(PATH,label),write("sources/entry.json",source),write("verify.py",original),
                 tool("run_shell",command=command),{"text":"[BLOCKED] Named date assertion failed."},
                 tool("delegate_task",assignee="builder",objective="Investigate the failure without fabricating dates."),
                 tool("run_shell",command=command),tool("run_browser",code=browser_code),
                 {"text":"[BLOCKED] Original assertion and a browser harness error preserved for independent review."},
                 tool("delegate_task",assignee="builder",objective="Resolve remaining checker dispute.",deliverable_paths=[PATH]),
                 tool("run_python",code=independent),
                 tool("submit_review",candidate_version=digest(files),results=[finding,prior,browser_finding]),
                 {"text":"Independently adjudicated the original named failure."},{"text":"Delivered."}]
        captured = []
        open_review = ReviewQueue.open
        def capture(queue, *args, **kwargs):
            review = open_review(queue, *args, **kwargs)
            captured.append(review)
            return review
        browser_error = "exit_code=1\nstderr:\nNameError: name 'js_errors' is not defined"
        with patch.object(ReviewQueue,"open",capture), patch("miniagent.tools.run_browser",
                return_value=ToolResult.failure(browser_error,exit_code=1,truncated=False)):
            detail,reports = self.run_steps(steps,acceptance_checks=[{"kind":"contains","path":PATH,"text":"10 units"}])
        self.assertEqual(detail["run"]["status"],"completed",reports)
        self.assertEqual([r["role"] for r in reports],["builder","builder","inspector"])
        self.assertTrue(reports[0]["evidence"]["observed_failure"])
        self.assertTrue(reports[1]["evidence"]["observed_failure"])
        self.assertEqual(captured[-1].objective,reports[0]["objective"])
        probe = captured[-1].probes[disputed]
        self.assertEqual(probe["code"],original)
        self.assertEqual(probe["sha256"],source_sha)
        self.assertEqual(probe["exit_code"],1)
        self.assertIn("snapshot date",probe["case_label"])
        self.assertEqual(probe["task_id"],reports[0]["task_id"])
        self.assertEqual(captured[-1].probes[browser_scope]["code"],browser_code)
        self.assertEqual(captured[-1].probes[browser_scope]["error"],browser_error)
        self.assertEqual((self.local/"verify.py").read_text(),original)
        self.assertEqual((self.local/"sources/entry.json").read_text(),source)
        self.assertEqual(reports[-1]["native_review"]["state"],"passed")

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

    def test_private_reviewer_can_test_in_temp_without_polluting_candidate(self):
        detail, reports = self.run_steps(initial() + [
            write("tests/peer_check.py", "print('independent test')"),
            tool("run_python", code="import tempfile\nfrom pathlib import Path\n"
                 "with tempfile.TemporaryDirectory() as directory:\n"
                 "    script = Path(directory, 'peer_check.py')\n"
                 "    script.write_text('assert 2 + 2 == 4')\n"
                 "    exec(script.read_text())"),
            submit(OLD), {"text":"Independently verified the original."}, {"text":"Delivered."}])
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertTrue(reports[-1]["published"])
        self.assertFalse((self.local / "tests/peer_check.py").exists())
        self.assertFalse(any(e["kind"] == "human_question" for e in detail["room_events"]))
        denied = [e for e in detail["events"] if e["kind"] == "tool_result" and e.get("tool") == "write_file" and not e["ok"]]
        self.assertTrue(any("tempfile" in e["preview"] for e in denied), denied)

    def test_reviewer_addition_is_not_part_of_approved_candidate(self):
        # A subprocess can create files outside the guarded file tools. Signing
        # must still detect an actual change to the private snapshot.
        _,reports=self.run_steps(initial()+[tool("run_python", code="from pathlib import Path\nPath('notes.md').write_text('unexpected')"),
            submit(OLD),{"text":"[BLOCKED] Tree changed."},{"text":"[BLOCKED] No reviewed version."}])
        self.assertFalse(reports[-1]["published"])
        self.assertIsNone(reports[-1]["native_review"]["assessment"])
        self.assertFalse((self.local/"notes.md").exists())
        self.assertFalse((self.local/PATH).exists())

    def test_concurrent_public_edit_and_checkpoint_tampering_are_rejected(self):
        for tamper in ("base","checkpoint"):
            with self.subTest(tamper=tamper):
                publish_original = RoomCrew._publish
                mutated = []
                def mutate(crew, assignment, role, original, *, candidate=None, reviewer_probe=False):
                    self.assertFalse(reviewer_probe)
                    if candidate:
                        self.assertEqual(role, "inspector")
                        # This seam runs after the signed review and before the
                        # real publisher validates or commits either tree.
                        # Keep this race independent of model final-turn counts.
                        path = (crew.path/"agents"/crew.lead/"records.json" if tamper=="base" else
                                candidate["checkpoint"]/"agents"/candidate["role"]/PATH)
                        path.write_text("Concurrent edit preserved.")
                        mutated.append(path)
                    return publish_original(crew, assignment, role, original, candidate=candidate, reviewer_probe=reviewer_probe)
                with patch.object(RoomCrew, "_publish", mutate):
                    _,reports=self.run_steps(initial()+[submit(OLD),{"text":"Verified."},
                                                       {"text":"[BLOCKED] No current version."}])
                self.assertEqual(len(mutated), 1)
                self.assertEqual(reports[-1]["native_review"]["assessment"]["verdict"], "passed")
                self.assertFalse(reports[-1]["published"],reports)
                self.assertEqual(reports[-1]["native_review"]["state"],"blocked")
                self.assertFalse((self.local/PATH).exists())
                if tamper=="base":
                    self.assertEqual(mutated[0].read_text(),"Concurrent edit preserved.")

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
