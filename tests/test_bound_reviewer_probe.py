"""Only failures cited by a signed product finding need linked probe review."""

import ast
import hashlib
import importlib.util
import os
import json
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.crew_progress import _check_key, _script_inputs, _script_layout, _shell_case_suite
from miniagent.research import Page
from miniagent.studio_reviews import Review, ReviewSession, Rule, digest
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_native_review import PATH
from tests.test_studio_service import finished, request


LABEL = "Shipment quantity: 10 units. Confirmed warehouse."
RULE = {"id": "quantity", "method": "execute",
        "description": "Independently verify the label records exactly 10 shipped units."}
NOTE = "The old probe added ten by mistake. Verify the original label and calculation independently."
SOURCE_URL = "https://issuer.example/dispatch"
SOURCE_RULE = {"id": "quantity", "method": "source",
               "description": "Confirm the shipped quantity against the issuer's dispatch record."}
BAD_PROBE = (
    "from pathlib import Path\nimport re\n"
    "label = Path('delivery.txt').read_text()\n"
    "observed = int(re.search(r'quantity: (\\d+)', label).group(1)) + 10\n"
    "assert observed == 10, observed\n"
)
FIXED_PROBE = (
    "from pathlib import Path\nimport re\n"
    "label = Path('delivery.txt').read_text()\n"
    "actual = int(re.search(r'quantity: (\\d+)', label).group(1))\n"
    "assert actual == 10, actual\n"
    "print('PASS corrected quantity')\n"
)
PEER_PROBE = (
    "from pathlib import Path\n"
    "quantity = int(Path('delivery.txt').read_text().split('quantity: ')[1].split()[0])\n"
    "assert quantity == 10, quantity\n"
    "print('PASS independent quantity')\n"
)


def scope(code):
    return _check_key(_script_inputs(code)[1])


BAD_SCOPE, FIXED_SCOPE, PEER_SCOPE = map(scope, (BAD_PROBE, FIXED_PROBE, PEER_PROBE))


def product_version(label=LABEL):
    return digest({PATH: hashlib.sha256(label.encode()).hexdigest()})


def bound_submission(label=LABEL, bad_probe=BAD_PROBE, fixed_probe=FIXED_PROBE):
    return tool("submit_review", candidate_version=product_version(label), results=[{
        "criterion_id": RULE["id"], "verdict": "passed",
        "reason": "The original failed calculation added ten; the corrected assertion checks the shipment quantity.",
        "evidence": ["file:" + PATH, "fail:" + scope(bad_probe), "pass:" + scope(fixed_probe)],
    }])


class ProbeProvider(RecordingProvider):
    """Use the host's bound candidate version, never invent a review identifier."""

    def __init__(self, script):
        super().__init__(script)
        self.probe_contexts = []

    @staticmethod
    def probe_context(messages):
        decoder = json.JSONDecoder()
        for message in reversed(messages):
            text = message.get("content")
            if not isinstance(text, str):
                continue
            for segment in reversed(text.split("材料：")[1:]):
                try:
                    data, _ = decoder.raw_decode(segment.lstrip())
                except (ValueError, TypeError):
                    continue
                if (isinstance(data, dict) and isinstance(data.get("candidate_version"), str)
                        and any(c.get("id", "").startswith("probe_") for c in data.get("criteria", []))):
                    return data
        return None

    def chat(self, messages, tools=None, temperature=0):
        if self.cursor < len(self.script) and "_probe_finding" in self.script[self.cursor]:
            step = self.script[self.cursor]
            finding = step["_probe_finding"]
            context = self.probe_context(messages)
            if context and "submit_review" in {t.name for t in tools or []}:
                self.probe_contexts.append(context)
                findings = [finding]
                findings.extend(step.get("_extra_findings", []))
                if step.get("_source_original"):
                    findings.append({"criterion_id": "original_" + SOURCE_RULE["id"], "verdict": "passed",
                                     "reason": "The fresh issuer record confirms the unchanged product's quantity.",
                                     "evidence": ["file:" + PATH, "source:" + SOURCE_URL]})
                self.script[self.cursor] = tool("submit_review", candidate_version=context["candidate_version"],
                                                results=findings)
            else:
                self.script[self.cursor] = {"text": "[BLOCKED] No bound independent probe-review session was offered."}
        return super().chat(messages, tools, temperature)


class BoundReviewerProbeTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("bound-reviewer-probe")
        self.service = StudioService(self.root / "runs", Settings(api_key="test-only"))
        self.addCleanup(cleanup, self.root)
        self.addCleanup(self.service.close)

    def run_steps(self, steps, *, arbiter=True, lead="lead", human_answer=None,
                  objective="Deliver a shipment label recording exactly ten units, independently checked.", **options):
        provider = ProbeProvider(steps)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.service.start(request(
                "team", conversation=roster(*dict.fromkeys([lead, "builder", "inspector", *(["arbiter"] if arbiter else [])])),
                objective=objective,
                acceptance_files=options.pop("acceptance_files", [PATH]), limits={"max_steps": 90}, **options,
            ))
            detail = finished(self.service, run["id"])
            if human_answer is not None:
                self.assertEqual(detail["run"]["status"], "waiting_user")
                question, = detail["pending_questions"]
                self.service.reply_human(run["id"], question["request_id"], human_answer)
                detail = finished(self.service, run["id"])
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        return detail, reports, provider

    @staticmethod
    def start_bound_review(*, label=LABEL, bad_probe=BAD_PROBE, fixed_probe=FIXED_PROBE, final_reply=True):
        steps = [
            tool("delegate_task", assignee="builder", objective="Write the ten-unit shipment label.",
                 deliverable_paths=[PATH], remaining_roles=["inspector"]),
            write(PATH, label), {"text": "The label is published."},
            tool("delegate_task", assignee="inspector", objective="Independently check the quantity.",
                 deliverable_paths=["inspection.md"], remaining_roles=[],
                 review={"candidate": "builder", "criteria": [RULE]}),
            tool("read_file", path=PATH), write("inspection.md", NOTE),
            tool("run_python", code=bad_probe), tool("run_python", code=fixed_probe),
            bound_submission(label, bad_probe, fixed_probe),
        ]
        if final_reply:
            steps.append({"text": "[BLOCKED] The product finding cites my failed probe; another member must adjudicate it."})
        return steps

    @staticmethod
    def peer_steps(*, assignee="arbiter", evidence=None, peer_probe=PEER_PROBE, verdict="passed"):
        finding = {"criterion_id": "probe_" + BAD_SCOPE, "verdict": verdict,
                   "reason": "The original probe added ten. I independently read the unchanged label and checked its quantity.",
                   "evidence": evidence if evidence is not None else ["pass:" + scope(peer_probe)]}
        if verdict != "passed":
            finding.update(cause="product", action="The original product author must correct the shipped quantity.")
        return [
            tool("delegate_task", assignee=assignee, objective="Independently resolve the outstanding bound probe."),
            tool("read_file", path=PATH), tool("read_file", path="inspection.md"),
            tool("run_python", code=peer_probe), {"_probe_finding": finding},
        ]

    def test_bound_failed_probe_is_independently_adjudicated_then_original_review_resumes(self):
        steps = self.start_bound_review() + self.peer_steps() + [
            {"text": "Independently resolved the reviewer's mistaken probe on the frozen tree."},
            {"text": "Delivered the unchanged label with the original independent product review."},
        ]
        detail, reports, provider = self.run_steps(steps)
        self.assertEqual(detail["run"]["status"], "completed",
                         [(r["role"], r["remaining_issue"], r.get("native_review")) for r in reports])
        child, = [r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        native = child["native_review"]
        author, original = reports[:2]
        self.assertEqual((native["target"], native["probe_parent"]), (original["task_id"], author["task_id"]))
        self.assertEqual(child["role"], "arbiter")
        self.assertEqual([c["id"] for c in native["criteria"]], ["probe_" + BAD_SCOPE])
        self.assertEqual(original["native_review"]["assessment"]["evaluator"], "inspector")
        self.assertEqual([c["id"] for c in original["native_review"]["criteria"]], [RULE["id"]])
        self.assertTrue(provider.probe_contexts)
        public = self.root / "runs" / detail["run"]["id"] / "agents" / "lead" / PATH
        self.assertEqual(public.read_text(), LABEL)
        contexts = json.dumps(provider.probe_contexts, ensure_ascii=False)
        self.assertIn(BAD_SCOPE, contexts)

    def test_missing_failure_receipt_rejected_before_handoff_then_adjudicated_on_same_candidate(self):
        steps = self.start_bound_review()
        unbound = bound_submission()
        unbound["tool_calls"][0]["arguments"]["results"][0]["evidence"] = ["file:" + PATH, "pass:" + FIXED_SCOPE]
        steps[-2:-2] = [unbound, tool("list_review_evidence"), tool("read_lines", path=PATH)]
        steps += self.peer_steps() + [{"text":"Verified the actual label independently."}, {"text":"Delivered."}]
        detail, reports, provider = self.run_steps(steps)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        original, arbiter = reports[1:]
        self.assertEqual(original["native_review"]["assessment"]["verdict"], "passed")
        self.assertEqual(arbiter["native_review"]["state"], "passed")
        path = self.root / "runs" / detail["run"]["id"] / "trace.jsonl"
        trace = [json.loads(line) for line in path.read_text().splitlines()]
        calls = [r for r in trace if r.get("kind") == "tool_result" and r.get("task_id") == original["task_id"]]
        submissions = [r for r in calls if r.get("tool") == "submit_review"]
        self.assertEqual([r["ok"] for r in submissions], [False, True])
        failed = original["evidence"]["failed_check_details"][0]
        rejection = next(m["content"] for messages, _ in provider.requests for m in messages
                         if m.get("role") == "tool" and "全通过签署尚未生效" in str(m.get("content")))
        self.assertIn(failed["tool_call_id"], rejection)
        bound = original["native_review"]["assessment"]["results"][0]["receipts"]
        self.assertIn(next(r["ref"] for r in bound if r["kind"] == "failed_check"), rejection)
        self.assertTrue(any(r["ok"] and r.get("tool") == "read_lines" for r in calls))
        self.assertEqual([e["text"] for e in detail["room_events"] if e["kind"] == "reviewer_probe_resolved"],
                         ["probe_corrected"])

    def test_unprocessed_probe_preserves_reason_when_reviewer_cannot_finish(self):
        steps = self.start_bound_review()
        steps[-2]["tool_calls"][0]["arguments"]["results"][0]["evidence"] = ["file:" + PATH, "pass:" + FIXED_SCOPE]
        steps += [{"text":"[BLOCKED] The independent review could not finish."}]
        detail, reports, _ = self.run_steps(steps)
        self.assertNotEqual(detail["run"]["status"], "completed")
        review = reports[-1]["native_review"]
        self.assertIsNone(review["assessment"])
        self.assertEqual(review["state"], "blocked")
        self.assertIn("实跑失败", review["host_issue"])
        self.assertIn(BAD_SCOPE, review["host_issue"])

    def test_incomplete_original_source_cannot_gain_the_bound_probe_exception(self):
        oversized = BAD_PROBE + "# full source unavailable to the bounded probe ledger\n" * 700
        self.assertGreater(len(oversized.encode()), 32768)
        steps = self.start_bound_review(bad_probe=oversized) + [
            {"text": "[BLOCKED] The original probe cannot be fully disclosed for independent adjudication."},
        ]
        detail, reports, provider = self.run_steps(steps)
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertFalse(provider.probe_contexts)
        self.assertFalse(any(r.get("native_review", {}).get("kind") == "reviewer_probe" for r in reports))

    def test_exploration_then_fresh_positive_finding_does_not_create_an_extra_review(self):
        steps = self.start_bound_review()
        finding = steps[-2]["tool_calls"][0]["arguments"]["results"][0]
        finding.update(verdict="inconclusive", cause="verification",
                       evidence=["file:" + PATH, "fail:" + BAD_SCOPE],
                       action="Continue independent verification with the corrected computation.")
        steps[-1] = {"text": "The earlier computation was mistaken; the product criterion remains under review."}
        final_finding = bound_submission()
        final_finding["tool_calls"][0]["arguments"]["results"][0]["evidence"] = [
            "file:" + PATH, "pass:" + FIXED_SCOPE,
        ]
        steps += [
            tool("delegate_task", assignee="inspector", objective="Finish the original independent quantity check."),
            tool("read_file", path=PATH), tool("run_python", code=FIXED_PROBE), final_finding,
            {"text": "Independently verified ten units with the current real assertion."},
            {"text": "Delivered the independently checked ten-unit label."},
        ]
        detail, reports, provider = self.run_steps(steps)
        self.assertEqual(detail["run"]["status"], "completed")
        self.assertEqual([r["role"] for r in reports], ["builder", "inspector", "inspector"])
        self.assertTrue(reports[1]["evidence"]["failed_check_details"], "Historical diagnostics remain visible.")
        self.assertFalse(provider.probe_contexts)
        self.assertFalse(any(r.get("native_review", {}).get("kind") == "reviewer_probe" for r in reports))

    def test_inconclusive_comparison_returns_peer_evidence_to_original_reviewer_for_new_signature(self):
        reference = "reference.txt"
        rule = {"id":"C1", "method":"inspect", "reference_path":reference,
                "description":"The quantity on the shipment must equal the original dispatch."}
        reference_body = "10"
        compare = ("from pathlib import Path\nqty = int(Path('delivery.txt').read_text().split('quantity: ')[1].split()[0])\n"
                   "expected = int(Path('reference.txt').read_text())\nassert qty == expected\nprint('PASS shipment matches dispatch')\n")
        original = self.start_bound_review(bad_probe=BAD_PROBE, fixed_probe=compare)
        original[3]["tool_calls"][0]["arguments"]["review"] = {"candidate":PATH, "criteria":[rule]}
        original.insert(-4, tool("read_file", path=reference))
        findings = original[-2]["tool_calls"][0]["arguments"]["results"]
        findings[0].update(criterion_id="C1", verdict="inconclusive", cause="verification", reason="The original probe added ten.",
                           action="Independently check the disputed calculation against the original dispatch.")
        findings[0]["evidence"].append("reference:" + reference)
        original[-1] = {"text":"[BLOCKED] The conflicting executed checks need an independent measurement."}
        steps = [tool("delegate_task", assignee="lead", objective="Record the initial dispatch.", deliverable_paths=[reference]),
                 write(reference, reference_body), {"text":"Recorded."}] + original
        peer = self.peer_steps()
        original_finding = {"criterion_id":"original_C1", "verdict":"passed", "reason":"Read the label and original ten-unit dispatch.",
                            "evidence":["file:" + PATH, "reference:" + reference]}
        peer[-1]["_extra_findings"] = [original_finding]
        # Reading the report, then a good assertion on the label alone, cannot
        # replace the explicitly bound original comparison.
        dual = {**peer[-1], "_probe_finding":{**peer[-1]["_probe_finding"], "evidence":["pass:" + scope(compare)]}}
        steps += peer + [tool("read_file", path=reference), peer[-1], tool("run_python", code=compare), dual,
                         {"text":"I independently adjudicated the competing checks."}]
        final = {"criterion_id":"C1", "verdict":"passed", "reason":"The remeasured shipment matches the frozen dispatch.",
                 "evidence":["file:" + PATH,"reference:" + reference,"pass:" + scope(compare)]}
        steps += [tool("delegate_task", assignee="inspector", objective="Re-sign C1 against its original reference."),
                  tool("read_file", path=PATH), tool("read_file", path=reference), tool("run_python", code=compare),
                  tool("submit_review", candidate_version=product_version(), results=[final]), {"text":"Delivered."}]
        detail, reports, provider = self.run_steps(steps)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        original, peer, final = [r for r in reports if r.get("native_review")]
        self.assertEqual([r["role"] for r in (original, peer, final)], ["inspector","arbiter","inspector"])
        self.assertEqual([r["native_review"]["assessment"]["verdict"] for r in (original, peer, final)],
                         ["inconclusive","passed","passed"])
        self.assertEqual(original["native_review"]["candidate_version"], final["native_review"]["candidate_version"])
        result = next(x for x in peer["native_review"]["assessment"]["results"] if x["criterion_id"] == "original_C1")
        self.assertEqual([(r["kind"], r["path"]) for r in result["receipts"]], [("read", PATH), ("reference", reference)])
        evidence = final["native_review"]["peer_recheck"]
        self.assertEqual((evidence["review_task"], evidence["evaluator"]), (peer["task_id"], "arbiter"))
        self.assertEqual(evidence["reference_files"], {reference:hashlib.sha256(reference_body.encode()).hexdigest()})
        self.assertEqual([e["text"] for e in detail["room_events"] if e["kind"] == "reviewer_probe_resolved"],
                         ["probe_corrected_pending_product_review"])
        self.assertIn("同时取值于绑定的对照原件 " + reference, str(provider.requests))

    @unittest.skipUnless(os.name == "posix" and importlib.util.find_spec("playwright"), "local Playwright required")
    def test_localhost_form_dispute_without_read_receipts_reaches_independent_recheck_and_resign(self):
        page_path = "static/index.html"
        html = '<form data-testid="booking-form"><input id="bookerInput"></form>'
        version = digest({page_path:hashlib.sha256(html.encode()).hexdigest()})
        rule = {"id":"startup", "method":"execute", "description":"The local booking form is usable."}
        def script(check):
            return ("import functools, threading\nfrom http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer\n"
                    "handler = functools.partial(SimpleHTTPRequestHandler, directory='static')\n"
                    "with ThreadingHTTPServer(('127.0.0.1',0),handler) as http:\n"
                    "    threading.Thread(target=http.serve_forever,daemon=True).start()\n"
                    "    try:\n        page.goto(f'http://127.0.0.1:{http.server_port}/')\n"
                    + "\n".join("        " + line for line in check.splitlines()) +
                    "\n    finally:\n        http.shutdown()\n")
        bad = script("import sys\nprint('real unabridged error: ' + 'X' * 8500, file=sys.stderr)\n"
                     "expect(page.locator('#booking-form')).to_be_visible(timeout=100)")
        corrected = script("form = page.get_by_test_id('booking-form')\nexpect(form).to_be_visible()")
        peer_test = script("expect(page.locator('#bookerInput')).to_be_editable()")
        def key(code):
            return _check_key("browser " + hashlib.sha256((".\0" + ast.dump(ast.parse(code))).encode()).hexdigest())
        steps = [
            tool("delegate_task", assignee="builder", objective="Create the booking form.", deliverable_paths=[page_path]),
            write(page_path, html), {"text":"Delivered booking form."},
            tool("delegate_task", assignee="inspector", objective="Verify startup and form.", deliverable_paths=["qa.md"],
                 review={"candidate":"builder", "criteria":[rule]}),
            write("qa.md", "Examined the live booking form using the wrong and then the real selector."),
            tool("run_browser", code=bad), tool("run_browser", code=corrected),
            tool("submit_review", candidate_version=version, results=[{"criterion_id":"startup","verdict":"inconclusive",
                "cause":"verification","reason":"The assertion for the ID failed, the actual test ID passed.",
                "action":"Independently check which selector matches the shipped form.",
                "evidence":["fail:"+key(bad), "pass:"+key(corrected)]}]),
            {"text":"[BLOCKED] The conflicting form checks need independent adjudication."},
            tool("delegate_task", assignee="arbiter", objective="Resolve the disputed form check."),
            tool("run_browser", code=peer_test),
            {"_probe_finding":{"criterion_id":"probe_"+key(bad), "verdict":"passed",
                "reason":"Verified the actual frozen booking input accepts text; the guessed form ID was wrong.",
                "evidence":["pass:"+key(peer_test)]}},
            {"text":"The form was verified on the frozen page."},
            tool("delegate_task", assignee="inspector", objective="Sign the original startup criterion."),
            tool("run_browser", code=corrected),
            tool("submit_review",candidate_version=version,results=[{"criterion_id":"startup","verdict":"passed",
                "reason":"I rechecked the actual booking form.", "evidence":["pass:"+key(corrected)]}]),
            {"text":"The original review has passed."},
            tool("delegate_task", assignee="lead", objective="Write the promised launch instructions.",
                 deliverable_paths=["README.md"]),
            write("README.md","Launch instructions for this checked booking form."),
            {"text":"Published the launch instructions."},
            {"text":"Delivered."},
        ]
        detail, reports, _ = self.run_steps(steps, acceptance_files=["README.md"],
            objective="Deliver a working local booking form and launch instructions in README.md.")
        if any("环境缺少 Chromium/Chrome" in str(r.get("evidence")) for r in reports):
            self.skipTest("a local Chromium or Chrome is required")
        self.assertEqual(detail["run"]["status"], "completed", [(r["role"],r["remaining_issue"],r.get("native_review")) for r in reports])
        signed = [r for r in reports if r.get("native_review")]
        self.assertEqual([r["role"] for r in signed], ["inspector","arbiter","inspector"])
        first,peer,last = [r["native_review"] for r in signed]
        self.assertEqual((first["assessment"]["verdict"],peer["state"],last["state"]),("inconclusive","passed","passed"))
        pending, = [e for e in detail["room_events"] if e["kind"]=="reviewer_probe_pending"]
        probe, = pending["probes"].values()
        self.assertTrue(probe["display_truncated"])
        self.assertIn("X"*8500, probe["error"])
        self.assertEqual(probe["code"], bad)
        receipts = first["assessment"]["results"][0]["receipts"]
        self.assertEqual({r["kind"] for r in receipts},{"failed_check","passed_check"})
        self.assertEqual([set(r["browser_inputs"]) for r in receipts],[{page_path},{page_path}])
        self.assertEqual(first["candidate_version"], last["candidate_version"])

    def test_unrelated_passing_probe_cannot_escalate_an_inconclusive_product_finding(self):
        unrelated = "assert 4 == 2 + 2\nprint('PASS arithmetic')\n"
        steps = self.start_bound_review(fixed_probe=unrelated)
        finding = steps[-2]["tool_calls"][0]["arguments"]["results"][0]
        finding.update(verdict="inconclusive", cause="verification", action="Have another person check the label.")
        steps += [{"text":"[BLOCKED] The positive arithmetic result does not validate the actual label."}]
        detail, reports, _ = self.run_steps(steps)
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertIn("冻结产品输入",reports[-1]["probe_review_issue"])
        signed = reports[-1]["native_review"]["assessment"]
        self.assertEqual(signed["verdict"], "inconclusive")
        receipts = signed["results"][0]["receipts"]
        self.assertEqual({r["kind"] for r in receipts}, {"read", "passed_check", "failed_check"})
        self.assertFalse(next(r for r in receipts if r["kind"] == "passed_check").get("asserted_inputs"))
        self.assertFalse(any(e["kind"] == "reviewer_probe_pending" for e in detail["room_events"]))

    def test_unrelated_browser_page_in_same_product_cannot_resolve_other_pages_failure(self):
        rule = Rule(id="startup",method="execute",description="Verify the booking form.")
        item = Review("product","builder","inspector","Check startup",[rule],{"form.html":"1"*64,"about.html":"2"*64})
        session = ReviewSession(item, dict(item.files), None, "attempt-test")
        finding = {"criterion_id":"startup","verdict":"inconclusive","cause":"verification","receipts":[
            {"kind":"failed_check", "browser_inputs":{"form.html":"1"*64}},
            {"kind":"passed_check", "asserted":True,"browser_inputs":{"about.html":"2"*64},
             "asserted_inputs":{"about.html":"2"*64}}]}
        self.assertIn("原失败页面",session.verification_conflict_issue([finding]))
        check = finding["receipts"][1]
        check.update(browser_inputs={"form.html":"0"*64},asserted_inputs={"form.html":"0"*64})
        self.assertIn("冻结产品输入",session.verification_conflict_issue([finding]))
        check.update(browser_inputs={"form.html":"1"*64},asserted_inputs={"form.html":"1"*64})
        self.assertEqual(session.verification_conflict_issue([finding]), "")

    def test_inconclusive_source_without_a_fetched_source_stays_with_original_reviewer(self):
        steps = self.start_bound_review()
        steps[3]["tool_calls"][0]["arguments"]["review"]["criteria"] = [SOURCE_RULE]
        finding = steps[-2]["tool_calls"][0]["arguments"]["results"][0]
        finding.update(verdict="inconclusive", cause="verification", action="Acquire the missing external dispatch.")
        steps += [{"text":"[BLOCKED] The issuer has not been inspected."}]
        detail, reports, _ = self.run_steps(steps, required_source_review_paths=[PATH])
        self.assertNotEqual(detail["run"]["status"], "completed")
        signed = reports[-1]["native_review"]["assessment"]
        self.assertEqual(signed["verdict"], "inconclusive")
        self.assertFalse(any(e["kind"] == "reviewer_probe_pending" for e in detail["room_events"]))

    def test_original_reviewer_cannot_be_selected_to_sign_its_own_probe(self):
        steps = self.start_bound_review() + self.peer_steps(assignee="inspector") + [
            {"text": "Independently checked the mistaken probe."},
            {"text": "Delivered the original independently reviewed product."},
        ]
        detail, reports, _ = self.run_steps(steps)
        self.assertEqual(detail["run"]["status"], "completed")
        child, = [r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        self.assertEqual(child["role"], "arbiter")
        self.assertEqual(child["native_review"]["reviewer"], "arbiter")
        self.assertEqual([c["id"] for c in child["native_review"]["criteria"]], ["probe_" + BAD_SCOPE])

    def test_product_author_is_rerouted_to_an_independent_arbiter(self):
        steps = self.start_bound_review() + self.peer_steps(assignee="builder") + [
            {"text": "A third member independently checked the bound product facts."},
            {"text": "Delivered the original independently reviewed product."},
        ]
        detail, reports, _ = self.run_steps(steps)
        self.assertEqual(detail["run"]["status"], "completed")
        child, = [r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        self.assertEqual(child["role"], "arbiter")
        self.assertNotIn(child["native_review"]["reviewer"], {"lead", "builder", "inspector"})

    def test_three_person_room_lead_can_adjudicate_the_reviewers_failed_probe_after_contributing_separate_notes(self):
        steps = [
            tool("delegate_task", assignee="lead", objective="Research the unit convention for the builder.",
                 deliverable_paths=["notes.md"], remaining_roles=["builder", "inspector"]),
            write("notes.md", "The warehouse counts physical units."), {"text": "Research notes published."},
            *self.start_bound_review(), *self.peer_steps(assignee="lead"),
            {"text": "Independently checked the mistaken probe."}, {"text": "Delivered the verified label."}]
        detail, reports, provider = self.run_steps(steps, arbiter=False)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertEqual([r["role"] for r in reports], ["lead", "builder", "inspector", "lead"])
        _, product, inspection, adjudication = reports
        dispute = adjudication["native_review"]
        self.assertTrue(provider.probe_contexts)
        self.assertEqual((dispute["reviewer"], dispute["target"], dispute["probe_parent"]),
                         ("lead", inspection["task_id"], product["task_id"]))
        self.assertTrue(adjudication["published"])
        self.assertEqual(dispute["files"][PATH], inspection["native_review"]["files"][PATH])
        self.assertEqual(dispute["assessment"]["candidate_version"], dispute["candidate_version"])
        self.assertNotEqual(adjudication["task_id"], detail["run"]["id"])

    def test_without_a_third_member_the_product_author_cannot_self_certify(self):
        steps = self.start_bound_review() + [
            tool("delegate_task", assignee="builder", objective="Resolve the outstanding bound probe."),
            {"text": "[BLOCKED] No third member can independently adjudicate the bound product facts."},
        ]
        detail, reports, provider = self.run_steps(steps, arbiter=False, lead="builder")
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertFalse(provider.probe_contexts)
        self.assertFalse(any(r.get("native_review", {}).get("kind") == "reviewer_probe" for r in reports))
        self.assertEqual([r["role"] for r in reports], ["builder", "inspector"])
        self.assertIn("没有合格成员", reports[-1]["native_review"]["host_issue"])

    def test_plain_assert_true_cannot_use_a_separate_product_read_as_its_input(self):
        unrelated = "assert True\nprint('PASS unrelated constant')\n"
        steps = self.start_bound_review() + self.peer_steps(peer_probe=unrelated) + [
            {"text": "[BLOCKED] A constant assertion does not test the bound product."},
            {"text": "[BLOCKED] No execution receipt establishes the original product facts."},
        ]
        detail, reports, provider = self.run_steps(steps)
        self.assertTrue(provider.probe_contexts)
        self.assertNotEqual(detail["run"]["status"], "completed")
        child, = [r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        self.assertIsNone(child["native_review"]["assessment"])

    def test_original_source_rule_needs_fresh_source_and_retains_original_criterion(self):
        steps = self.start_bound_review()
        steps[3]["tool_calls"][0]["arguments"]["review"]["criteria"] = [SOURCE_RULE]
        signed = steps[-2]["tool_calls"][0]["arguments"]["results"][0]
        signed["evidence"].append("source:" + SOURCE_URL)
        steps.insert(6, tool("http_get", url=SOURCE_URL))
        peer = self.peer_steps()
        first = peer[-1]
        first["_probe_finding"]["evidence"].extend(["file:" + PATH, "source:" + SOURCE_URL])
        # The host must reject omission of the inherited source criterion, then
        # reject a purported source receipt not fetched by this independent peer.
        second = {"_probe_finding": dict(first["_probe_finding"]), "_source_original": True}
        complete = {"_probe_finding": dict(first["_probe_finding"]), "_source_original": True}
        steps += peer + [second, tool("http_get", url=SOURCE_URL),
                         tool("run_python", code=PEER_PROBE), complete,
                         {"text": "Verified the frozen product against fresh issuer evidence and a real quantity assertion."},
                         {"text": "Delivered the source-verified shipment label."}]
        with patch("miniagent.research.fetch", return_value=Page(
                SOURCE_URL, "text/plain", LABEL.encode(), False)) as fetch:
            detail, reports, provider = self.run_steps(steps, required_source_review_paths=[PATH])
        self.assertEqual(detail["run"]["status"], "completed",
                         [(r["role"], r["remaining_issue"]) for r in reports])
        self.assertEqual(fetch.call_count, 2)
        child, = [r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        criteria = {c["id"]: c for c in child["native_review"]["criteria"]}
        self.assertEqual(set(criteria), {"probe_" + BAD_SCOPE, "original_quantity"})
        self.assertEqual(criteria["original_quantity"]["method"], "source")
        self.assertEqual(criteria["original_quantity"]["description"], SOURCE_RULE["description"])
        self.assertTrue(any("每个标准都须正好有一个结论" in str(messages) for messages, _ in provider.requests))
        self.assertTrue(any("引用了未观察到或不唯一的证据" in str(messages) for messages, _ in provider.requests))

    def test_named_file_probe_keeps_original_script_and_scope_for_source_adjudication(self):
        public_url = "https://public-inspection.example/documents/2026-01888"
        notice = json.dumps({"text": "The notice includes a link titled Public inspection draft.",
                             "links": [{"text": "Public inspection draft", "url": public_url}]})
        label = "The notice links to the public inspection draft: " + public_url
        rule = {"id": "notice", "method": "source",
                "description": "Confirm the notice points to the public inspection draft using its original source."}
        path = "checks/notice_link_check.py"
        command = "python3 " + path
        caption = "通告指向公示稿链接"
        old = (
            "import json\nfrom pathlib import Path\n"
            "record = json.loads(Path('notice.json').read_text())\n"
            f"PUBLIC = {public_url!r}\n"
            "failures = []\n"
            "def report(name, ok):\n"
            "    print(('ok' if ok else 'FAIL') + ' ' + name)\n"
            "    if not ok:\n"
            "        failures.append(name)\n"
            "print('BEGIN-COMPLETE-OUTPUT:' + 'x' * 3200)\n"
            "found = PUBLIC in record['text']\n"
            f"report({caption!r}, found)\n"
            "print('END-COMPLETE-OUTPUT:' + 'y' * 3200)\n"
            "assert not failures, failures\n"
            f"print('PASS ' + {caption!r})\n"
        )
        fixed = old.replace("found = PUBLIC in record['text']",
                            "found = any(link.get('url') == PUBLIC for link in record.get('links', []))")
        self.assertIsNone(_script_layout(old))
        self.assertIsNone(_script_layout(fixed))
        suite = _shell_case_suite(command, ".")
        self.assertIsNotNone(suite)
        old_sha, fixed_sha = (hashlib.sha256(code.encode()).hexdigest() for code in (old, fixed))
        old_scope = _check_key(suite + "\0sha256=" + old_sha, caption)
        fixed_scope = _check_key(suite + "\0sha256=" + fixed_sha, caption)
        self.assertNotEqual(old_scope, fixed_scope)
        version = digest({PATH: hashlib.sha256(label.encode()).hexdigest(),
                          "notice.json": hashlib.sha256(notice.encode()).hexdigest()})
        steps = [
            tool("delegate_task", assignee="builder", objective="Publish the notice link and extracted source.",
                 deliverable_paths=[PATH, "notice.json"], remaining_roles=["inspector"]),
            write(PATH, label), write("notice.json", notice), {"text": "Published the notice and link."},
            tool("delegate_task", assignee="inspector", objective="Independently verify the notice's draft link.",
                 deliverable_paths=["inspection.md"], remaining_roles=[],
                 review={"candidate": "builder", "criteria": [rule]}),
            tool("read_file", path=PATH), tool("read_file", path="notice.json"),
            write("inspection.md", "The original checker searched text instead of the structured links URL field."),
            tool("http_get", url=SOURCE_URL), write(path, old), tool("run_shell", command=command),
            write(path, fixed), tool("run_shell", command=command),
            tool("submit_review", candidate_version=version, results=[{
                "criterion_id": "notice", "verdict": "passed",
                "reason": "The original named FAIL came from searching text rather than links[].url; the source includes the link.",
                "evidence": ["file:" + PATH, "source:" + SOURCE_URL,
                             "fail:" + old_scope, "pass:" + _check_key(command)],
            }]),
            {"text": "[BLOCKED] The signed source finding cites the original named failure for independent adjudication."},
            tool("delegate_task", assignee="arbiter", objective="Resolve the bound notice checker discrepancy independently."),
            tool("read_file", path=PATH), tool("read_file", path="notice.json"),
            tool("read_file", path="inspection.md"), tool("http_get", url=SOURCE_URL),
            {"_probe_finding": {
                "criterion_id": "probe_" + old_scope, "verdict": "passed",
                "reason": "The frozen old script incorrectly searched text; fresh source and frozen product both include the link URL.",
                "evidence": ["file:" + PATH, "source:" + SOURCE_URL],
            }, "_extra_findings": [{
                "criterion_id": "original_notice", "verdict": "passed",
                "reason": "The freshly fetched issuer notice independently contains the same public draft link.",
                "evidence": ["file:" + PATH, "source:" + SOURCE_URL],
            }]},
            {"text": "Resolved the named original checker against the frozen product and fresh source."},
            {"text": "Delivered the source-verified notice link with the original signed review restored."},
        ]
        with patch("miniagent.research.fetch", return_value=Page(
                SOURCE_URL, "application/json", notice.encode(), False)) as fetch:
            detail, reports, provider = self.run_steps(
                steps, objective="Publish and independently verify the notice's link to its public inspection draft.",
                required_source_review_paths=[PATH])
        self.assertEqual(detail["run"]["status"], "completed",
                         [(r["role"], r["remaining_issue"], r.get("native_review")) for r in reports])
        self.assertEqual(fetch.call_count, 2)
        original = reports[1]
        self.assertIn(old_scope, str(original["evidence"]["failed_check_details"]))
        self.assertNotIn(fixed_scope, str(original["evidence"]["failed_check_details"]))
        event, = [e for e in detail["room_events"] if e["kind"] == "reviewer_probe_pending"]
        frozen = event["probes"][old_scope]
        self.assertEqual(set(event["probes"]), {old_scope})
        self.assertEqual((frozen["kind"], frozen["tool"], frozen["path"]), ("edited_script", "run_shell", path))
        self.assertEqual((frozen["sha256"], frozen["code"], frozen["exit_code"]), (old_sha, old, 1))
        self.assertIn("FAIL " + caption, frozen["error"])
        self.assertIn("AssertionError", frozen["error"])
        self.assertIn("BEGIN-COMPLETE-OUTPUT:" + "x" * 3200, frozen["error"])
        self.assertIn("END-COMPLETE-OUTPUT:" + "y" * 3200, frozen["error"])
        self.assertGreater(len(frozen["error"].encode()), 6400)
        self.assertLessEqual(len(frozen["error"].encode()), 8192)
        self.assertIn(caption, frozen["case_label"])
        self.assertEqual(frozen["task_id"], original["task_id"])
        child, = [r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        self.assertEqual(child["role"], "arbiter")
        criteria = {c["id"]: c for c in child["native_review"]["criteria"]}
        self.assertEqual(set(criteria), {"probe_" + old_scope, "original_notice"})
        self.assertEqual(criteria["probe_" + old_scope]["method"], "source")
        self.assertEqual(criteria["original_notice"]["method"], "source")
        self.assertEqual(criteria["original_notice"]["description"], rule["description"])
        self.assertTrue(provider.probe_contexts)
        public = self.root / "runs" / detail["run"]["id"] / "agents" / "lead"
        self.assertEqual((public / PATH).read_text(), label)
        self.assertEqual((public / "notice.json").read_text(), notice)
        trace = [json.loads(line) for line in (public.parents[1] / "trace.jsonl").read_text().splitlines()]
        original_result, = [row for row in trace if row["kind"] == "tool_result"
                            and row.get("tool_call_id") == frozen["tool_call_id"]]
        self.assertTrue(original_result["truncated"], "Model-facing output remains bounded.")

    def test_reading_and_a_clean_process_exit_cannot_certify_the_probe(self):
        steps = self.start_bound_review() + self.peer_steps(
            peer_probe="print('Process completed.')\n", evidence=["file:" + PATH],
        ) + [
            {"text": "[BLOCKED] I did not execute an independent assertion."},
            {"text": "[BLOCKED] The bound probe has no independent assertion receipt."},
        ]
        detail, reports, provider = self.run_steps(steps)
        self.assertTrue(provider.probe_contexts, "The negative case must reach the linked review.")
        self.assertNotEqual(detail["run"]["status"], "completed")
        child, = [r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        self.assertIsNone(child["native_review"]["assessment"])
        self.assertTrue(any("实际断言结果" in str(messages) for messages, _ in provider.requests))

    def test_a_different_scope_cannot_close_the_bound_failure(self):
        peer = self.peer_steps()
        peer[-1]["_probe_finding"]["criterion_id"] = "probe_" + FIXED_SCOPE
        detail, reports, provider = self.run_steps(self.start_bound_review() + peer + [
            {"text": "[BLOCKED] The submitted scope is not the disputed original probe."},
            {"text": "[BLOCKED] The original bound scope has not been adjudicated."},
        ])
        self.assertTrue(provider.probe_contexts)
        self.assertNotEqual(detail["run"]["status"], "completed")
        child, = [r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        self.assertIsNone(child["native_review"]["assessment"])
        self.assertEqual([c["id"] for c in child["native_review"]["criteria"]], ["probe_" + BAD_SCOPE])

    def test_verification_change_request_remains_with_the_independent_probe_reviewer(self):
        first = self.peer_steps(verdict="changes_requested", evidence=["file:" + PATH])
        first[-1]["_probe_finding"].update(cause="verification", reason="I need to recheck my calculation.",
                                           action="Rerun the quantity check on the frozen label.")
        steps = self.start_bound_review() + first + [{"text": "My verification needs correction."}]
        steps += self.peer_steps() + [{"text": "Confirmed the frozen quantity."}, {"text": "Delivered the reviewed label."}]
        detail, reports, _ = self.run_steps(steps)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        children = [r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        self.assertEqual([r["role"] for r in children], ["arbiter", "arbiter"])
        self.assertEqual([r["native_review"]["state"] for r in children], ["review", "passed"])
        decisions = [e["text"] for e in detail["room_events"] if e["kind"] == "reviewer_probe_resolved"]
        self.assertEqual(decisions, ["probe_corrected"])

    def test_adjudicator_can_report_its_own_probe_error_and_remeasure_from_same_frozen_input(self):
        bad = BAD_PROBE.replace(" + 10", " + 2").replace("observed == 10", "(observed, 'units') == (10, 'units')")
        self.assertNotEqual(scope(bad), BAD_SCOPE)
        first = self.peer_steps()
        first[0]["tool_calls"][0]["arguments"]["deliverable_paths"] = ["arbiter-notes.md"]
        first[-2:-2] = [write("notes/unrequested.md", "An unrequested public file would invalidate the snapshot."),
                        write("arbiter-notes.md", "A separately assigned note."),
                        tool("run_python", code=bad)]
        first.append({"_probe_finding":{
            "criterion_id":"probe_" + BAD_SCOPE, "verdict":"inconclusive", "cause":"verification",
            "evidence":["fail:" + scope(bad), "pass:" + PEER_SCOPE],
            "reason":"My own probe added two to the observed quantity.",
            "action":"Read and remeasure the same frozen label in a fresh review assignment."}})
        steps = self.start_bound_review(final_reply=False) + first + [{"text":"The quantity needs remeasurement."}]
        steps += self.peer_steps() + [{"text":"Confirmed the unchanged shipment quantity."}, {"text":"Delivered."}]
        detail, reports, provider = self.run_steps(steps)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        children = [r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        pending, completed = children
        self.assertEqual([r["role"] for r in children], ["arbiter", "arbiter"])
        original = next(r for r in reports if r["role"] == "inspector")
        self.assertEqual(original["signed_handoff"]["source"], "signed_review")
        self.assertFalse(original["published"])
        self.assertEqual([r["native_review"]["assessment"]["verdict"] for r in children], ["inconclusive", "passed"])
        self.assertEqual(pending["native_review"]["candidate_version"], completed["native_review"]["candidate_version"])
        failed, = pending["evidence"]["failed_check_details"]
        evidence = pending["native_review"]["assessment"]["results"][0]["receipts"]
        cited = next(r for r in evidence if r["kind"] == "failed_check")
        self.assertEqual((cited["scope"], cited["tool_call_id"]), (scope(bad), failed["tool_call_id"]))
        self.assertFalse(completed["evidence"]["failed_check_details"])
        feedback = [m["content"] for messages, _ in provider.requests for m in messages if m.get("role") == "tool"]
        self.assertTrue(any("会改变私有裁决的冻结快照" in m and "arbiter-notes.md" in m for m in feedback))
        self.assertTrue(any("已写入 arbiter-notes.md" in m for m in feedback))
        self.assertTrue(any("同一位裁决者" in m and cited["ref"] in m for m in feedback))
        checks = [e for e in detail["room_events"] if e["kind"] == "reviewer_probe_pending"]
        self.assertEqual(len(checks), 1, "The adjudicator's local probe must not recursively open a new review.")

    def test_addressed_lead_resumes_same_adjudicator_before_touching_frozen_report(self):
        initial = "Earlier public inspection report."
        prefix = [tool("delegate_task", assignee="inspector", objective="Publish the initial inspection note.",
                       deliverable_paths=["inspection.md"]), write("inspection.md", initial), {"text":"Published."}]
        first = self.peer_steps(verdict="inconclusive", evidence=["file:" + PATH])
        first[-1]["_probe_finding"].update(cause="verification", action="Remeasure from the same frozen label.")
        steps = prefix + self.start_bound_review(final_reply=False) + first + [{"text":"The quantity needs independent remeasurement."},
            tool("append_file", path="inspection.md", content="Premature coordinator edit."),
            tool("edit_file", path=PATH, old_text="10", new_text="11"), tool("read_file", path="inspection.md")]
        steps += self.peer_steps() + [{"text":"Confirmed the frozen quantity."},
            tool("append_file", path="inspection.md", content=" Coordinator recorded the closure."), {"text":"Delivered."}]
        detail, reports, provider = self.run_steps(steps, objective="@lead Coordinate the label and its independent review.")
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        child = [r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        self.assertEqual([r["role"] for r in child], ["arbiter", "arbiter"])
        version = child[0]["native_review"]["candidate_version"]
        self.assertEqual(child[1]["native_review"]["candidate_version"], version)
        ctx = [m["content"] for messages, _ in provider.requests for m in messages if isinstance(m.get("content"), str)]
        self.assertTrue(any("独立裁决正在等待 arbiter" in m and version in m and "省略 review 和 recover_from" in m for m in ctx))
        run = self.root / "runs" / detail["run"]["id"]
        events = [json.loads(line) for line in (run / "trace.jsonl").read_text().splitlines()]
        writes = [(e["tool"], e["ok"]) for e in events if e.get("task_id") == detail["run"]["id"]
                  and e["kind"] == "tool_result" and e["tool"] in {"append_file", "edit_file"}]
        self.assertEqual(writes, [("append_file", False), ("edit_file", False), ("append_file", True)])
        result = (run / "agents" / "lead" / "inspection.md").read_text()
        self.assertNotIn("Premature coordinator edit", result)
        self.assertIn("Coordinator recorded the closure", result)

    def test_changed_public_bytes_keep_probe_blocked_and_identify_the_changed_file(self):
        first = self.peer_steps(verdict="inconclusive", evidence=["file:" + PATH])
        first[-1]["_probe_finding"].update(cause="verification", action="Remeasure the frozen label.")
        steps = self.start_bound_review(final_reply=False) + first + [{"text":"The quantity needs remeasurement."},
            tool("run_python", code="from pathlib import Path\nPath('inspection.md').write_text('new public text')"),
            tool("delegate_task", assignee="arbiter", objective="Resume original adjudication."),
            {"text":"[BLOCKED] The public review report was changed."}]
        detail, reports, provider = self.run_steps(steps, objective="@lead Coordinate the quantity verification.")
        self.assertNotEqual(detail["run"]["status"], "completed")
        children = [r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        self.assertEqual([r["native_review"]["assessment"]["verdict"] for r in children], ["inconclusive"])
        text = [m["content"] for messages, _ in provider.requests for m in messages if m.get("role") == "tool"]
        self.assertTrue(any("此次冻结裁决已失效" in m and "公开基线差异" in m and "inspection.md" in m for m in text))
        self.assertFalse(any(e["kind"] == "reviewer_probe_resolved" for e in detail["room_events"]))

    def test_business_answer_resumes_the_same_frozen_probe_without_a_second_question(self):
        first = self.peer_steps(verdict="inconclusive", evidence=["file:" + PATH])
        first[-1]["_probe_finding"].update(cause="business", reason="The unit label has two plausible meanings.",
                                           action="Ask whether quantity means individual units.")
        steps = self.start_bound_review() + first + [
            {"text": "The owner must specify the unit."},
            tool("ask_human", question="Does quantity refer to individual units?")]
        steps += self.peer_steps() + [{"text": "Confirmed ten individual units."}, {"text": "Delivered the reviewed label."}]
        detail, reports, _ = self.run_steps(steps, human_answer="Use individual units.")
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        children = [r["native_review"] for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        self.assertEqual([item["state"] for item in children], ["awaiting_user", "passed"])
        self.assertEqual(children[0]["candidate_version"], children[1]["candidate_version"])
        self.assertEqual(len([e for e in detail["room_events"] if e["kind"] == "human_question"]), 1)

    def test_real_product_failure_returns_to_original_product_author(self):
        wrong_label = LABEL.replace("10 units", "9 units")
        weak_correction = FIXED_PROBE.replace("assert actual == 10", "assert actual == 9")
        steps = self.start_bound_review(label=wrong_label, fixed_probe=weak_correction)
        # A real failed assertion can support further investigation; a tentative
        # conclusion must not be published as a confirmed product defect.
        peer = self.peer_steps(evidence=["fail:" + PEER_SCOPE], verdict="inconclusive")
        steps += peer + [self.peer_steps(evidence=["fail:" + PEER_SCOPE], verdict="changes_requested")[-1]]
        steps += [
            {"text": "The label actually contains nine units. The product author must correct it."},
            tool("delegate_task", assignee="inspector", objective="Resolve the confirmed product discrepancy."),
            tool("read_file", path=PATH),
            {"text": "[BLOCKED] The product author still needs to correct the nine-unit label."},
            {"text": "[BLOCKED] The product's original quantity criterion is unresolved."},
        ]
        detail, reports, provider = self.run_steps(steps)
        self.assertTrue(provider.probe_contexts)
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertTrue(any("争议尚未查实" in str(messages) for messages, _ in provider.requests))
        child, = [r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        self.assertEqual(child["native_review"]["assessment"]["verdict"], "changes_requested")
        self.assertEqual(reports[-1]["role"], "builder")
        self.assertEqual(reports[-1]["native_review"]["author"], "builder")
        self.assertEqual([c["id"] for c in reports[-1]["native_review"]["criteria"]], [RULE["id"]])
        public = self.root / "runs" / detail["run"]["id"] / "agents" / "lead" / PATH
        self.assertEqual(public.read_text(), wrong_label)


if __name__ == "__main__":
    unittest.main()
