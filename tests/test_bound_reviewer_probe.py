"""Only failures cited by a signed product finding need linked probe review."""

import hashlib
import json
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.crew_progress import _check_key, _script_inputs, _script_layout, _shell_case_suite
from miniagent.research import Page
from miniagent.studio_reviews import digest
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

    def run_steps(self, steps, *, arbiter=True,
                  objective="Deliver a shipment label recording exactly ten units, independently checked.", **options):
        provider = ProbeProvider(steps)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.service.start(request(
                "team", conversation=roster("lead", "builder", "inspector", *(["arbiter"] if arbiter else [])),
                objective=objective,
                acceptance_files=[PATH], limits={"max_steps": 90}, **options,
            ))
            detail = finished(self.service, run["id"])
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        return detail, reports, provider

    @staticmethod
    def start_bound_review(*, label=LABEL, bad_probe=BAD_PROBE, fixed_probe=FIXED_PROBE):
        return [
            tool("delegate_task", assignee="builder", objective="Write the ten-unit shipment label.",
                 deliverable_paths=[PATH], remaining_roles=["inspector"]),
            write(PATH, label), {"text": "The label is published."},
            tool("delegate_task", assignee="inspector", objective="Independently check the quantity.",
                 deliverable_paths=["inspection.md"], remaining_roles=[],
                 review={"candidate": "builder", "criteria": [RULE]}),
            tool("read_file", path=PATH), write("inspection.md", NOTE),
            tool("run_python", code=bad_probe), tool("run_python", code=fixed_probe),
            bound_submission(label, bad_probe, fixed_probe),
            {"text": "[BLOCKED] The product finding cites my failed probe; another member must adjudicate it."},
        ]

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

    def test_without_a_third_member_the_product_author_cannot_self_certify(self):
        steps = self.start_bound_review() + [
            tool("delegate_task", assignee="builder", objective="Resolve the outstanding bound probe."),
            {"text": "[BLOCKED] No third member can independently adjudicate the bound product facts."},
        ]
        detail, reports, provider = self.run_steps(steps, arbiter=False)
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertFalse(provider.probe_contexts)
        self.assertFalse(any(r.get("native_review", {}).get("kind") == "reviewer_probe" for r in reports))
        self.assertEqual([r["role"] for r in reports], ["builder", "inspector"])

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

    def test_real_product_failure_returns_to_original_product_author(self):
        wrong_label = LABEL.replace("10 units", "9 units")
        weak_correction = FIXED_PROBE.replace("assert actual == 10", "assert actual == 9")
        steps = self.start_bound_review(label=wrong_label, fixed_probe=weak_correction)
        steps += self.peer_steps(evidence=["fail:" + PEER_SCOPE], verdict="changes_requested")
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
        child, = [r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        self.assertEqual(child["native_review"]["assessment"]["verdict"], "changes_requested")
        self.assertEqual(reports[-1]["role"], "builder")
        self.assertEqual(reports[-1]["native_review"]["author"], "builder")
        self.assertEqual([c["id"] for c in reports[-1]["native_review"]["criteria"]], [RULE["id"]])
        public = self.root / "runs" / detail["run"]["id"] / "agents" / "lead" / PATH
        self.assertEqual(public.read_text(), wrong_label)


if __name__ == "__main__":
    unittest.main()
