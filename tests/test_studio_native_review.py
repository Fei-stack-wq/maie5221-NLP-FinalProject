import hashlib
import json
import unittest
from unittest.mock import Mock, patch

from miniagent.config import Settings
from miniagent.research import Page
from miniagent.studio_reviews import Finding, Review, ReviewQueue, ReviewSession, Rule, Submission, digest
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

    def test_submit_review_exposes_the_same_wire_constraints_that_parse_findings(self):
        session = Mock(spec=ReviewSession)
        schema = ReviewSession.tool(session).spec().parameters
        root = Submission.model_json_schema()
        properties = schema["properties"]
        self.assertEqual(properties["candidate_version"], root["properties"]["candidate_version"])
        self.assertEqual(properties["candidate_version"]["pattern"], "^[a-f0-9]{64}$")
        results = properties["results"]
        self.assertEqual((results["minItems"], results["maxItems"]), (1, 24))
        self.assertEqual(results["items"], Finding.model_json_schema())
        fields = results["items"]["properties"]
        self.assertEqual((fields["reason"]["minLength"], fields["reason"]["maxLength"]), (1, 1500))
        self.assertEqual((fields["evidence"]["minItems"], fields["evidence"]["maxItems"]), (1, 8))
        self.assertIn({"type": "string", "maxLength": 1000}, fields["action"]["anyOf"])
        self.assertEqual(set(results["items"]["required"]), {"criterion_id", "verdict", "reason", "evidence"})
        self.assertIn("passed 时省略 cause 和 action", fields["verdict"]["description"])
        self.assertIn("business 仅可搭配 inconclusive", fields["cause"]["description"])

    def test_passing_finding_with_repair_fields_still_requires_a_corrected_signature(self):
        label = "Shipment destination: old warehouse. Verified dispatch record."
        detail, provider = self.execute(self.start_review() + [
            submit(label, "passed", cause="verification", action="Please recheck."),
            submit(label, "passed"), {"text":"Checked the candidate."}, {"text":"Delivered."}])
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        feedback = [message.get("content", "") for messages, _ in provider.requests
                    for message in messages if message["role"] == "tool"]
        self.assertTrue(any("通过项不应分派修复" in message for message in feedback))
        submissions = [row for row in detail["room_events"] if row["kind"] == "member_report"
                       and row["role"] == "inspector"]
        finding = submissions[0]["handoff"]["native_review"]["assessment"]["results"][0]
        self.assertEqual(finding["verdict"], "passed")
        self.assertIsNone(finding["action"])

    def execute(self, steps, *others, objective="Build and independently verify the shipment label.", **options):
        provider = RecordingProvider(steps)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.service.start(request("team",conversation=roster("lead","builder","inspector",*others),
                 objective=objective,acceptance_files=[PATH],
                 limits={"max_steps":90}, **options))
            detail = finished(self.service, run["id"])
        return detail, provider

    def start_review(self):
        return [tool("delegate_task", assignee="builder",objective="Write the shipment label.",deliverable_paths=[PATH],
                     remaining_roles=["inspector"]),
                write(PATH, "Shipment destination: old warehouse. Verified dispatch record."), {"text":"First label ready."},
                tool("delegate_task",assignee="inspector",objective="Review the finished label.",remaining_roles=[],
                     review={"candidate":"builder","criteria":[RULE]}),
                tool("read_file", path=PATH)]

    def test_reviewer_can_revise_its_published_report_when_resuming_the_same_candidate(self):
        project = self.root / "project"
        project.mkdir()
        manuscript = "Shipment destination: old warehouse. Verified dispatch record."
        report_path = "docs/review.md"
        first = "First review: independent confirmation still outstanding."
        final = "Final review: the shipping destination has been independently verified."
        steps = self.start_review()
        steps[3]["tool_calls"][0]["arguments"]["deliverable_paths"] = [report_path]
        steps += [write(report_path, first),
                  submit(manuscript, "inconclusive", cause="verification", action="Recheck the confirmed destination."),
                  {"text": "The initial report records the open verification question."},
                  tool("delegate_task", assignee="inspector", objective="Recheck the label and update your report.",
                       deliverable_paths=[report_path]),
                  tool("read_file", path=PATH), write(PATH, "replacement manuscript"), write(report_path, final),
                  submit(manuscript, "passed"), {"text": "Delivered the final guide and review."}]
        detail, provider = self.execute(steps, workspace_path=str(project))
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        events = [e for e in detail["room_events"] if e["kind"] == "member_report" and e["role"] == "inspector"]
        self.assertEqual([e["handoff"]["native_review"]["state"] for e in events], ["review", "passed"])
        snapshots = [next(a["path"] for a in e["artifacts"] if a["project_path"] == report_path) for e in events]
        self.assertEqual([self.service.artifact(detail["run"]["id"], path)["content"] for path in snapshots], [first, final])
        self.assertEqual((project / PATH).read_text(), manuscript)
        self.assertEqual((project / report_path).read_text(), final)
        self.assertIn("独立评审的冻结文件不可修改", str(provider.requests))

    def test_historical_checks_cannot_sign_until_rerun_on_current_workspace(self):
        report_path = "docs/review.md"
        manuscript = "Shipment destination: old warehouse. Verified dispatch record."
        for case, input_path, mutation, preserved in (
                ("read only", PATH, None, True),
                ("frozen input", PATH, report_path, False),
                ("report input", report_path, report_path, False),
                ("dynamic probe", PATH, report_path, False),
                ("fresh external material", PATH, "sources/new.md", False),
                ("changed assertion script", PATH, "check.py", False)):
            with self.subTest(case=case):
                code = (f"from pathlib import Path\ntext = Path({input_path!r}).read_text()\n"
                        + ("result = text.lower()\n" if case == "dynamic probe" else "")
                        + "assert 'warehouse' in text\nprint('PASS verifies warehouse')")
                steps = self.start_review()
                assignment = steps[3]["tool_calls"][0]["arguments"]
                assignment["deliverable_paths"] = [report_path]
                assignment["review"]["criteria"] = [{**RULE, "method": "execute"}]
                change = write(mutation, "The warehouse details have been checked.") if mutation else tool("read_file", path=PATH)
                steps += [write(report_path, "The warehouse is under review."), tool("run_python", code=code),
                          tool("list_review_evidence"), change,
                          tool("list_review_evidence"), {"sign": "first"}]
                if not preserved:
                    steps += [tool("run_python", code=code), tool("list_review_evidence"), {"sign": "first"},
                              {"sign": "alias" if case == "dynamic probe" else "latest"}]
                steps += [{"text": "Delivered the independently verified report."}]

                class SignedRef(RecordingProvider):
                    def chat(self, messages, tools=None, temperature=0):
                        next_step = self.script[self.cursor] if self.cursor < len(self.script) else {}
                        if "sign" in next_step:
                            catalogs = [json.loads(m["content"]) for m in messages if m["role"] == "tool"
                                        and m["content"].startswith('{"candidate_version"') and '"evidence": [' in m["content"]]
                            catalog = catalogs[0] if next_step["sign"] == "first" else catalogs[-1]
                            key = "alias" if next_step["sign"] == "alias" else "ref"
                            ref = next(r[key] for r in catalog["evidence"] if r["kind"] == "passed_check")
                            self.script[self.cursor] = tool("submit_review", candidate_version=version(manuscript),
                                results=[{"criterion_id":"content", "verdict":"passed",
                                          "reason":"Checked the warehouse with an executed assertion.", "evidence":[ref]}])
                        return super().chat(messages, tools, temperature)

                provider = SignedRef(steps)
                with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
                    run = self.service.start(request("team", conversation=roster("lead","builder","inspector"),
                        objective="Check the delivered records.", acceptance_files=[PATH], limits={"max_steps": 90}))
                    detail = finished(self.service, run["id"])
                self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
                trace = [json.loads(line) for line in (self.root/"runs"/run["id"]/"trace.jsonl").read_text().splitlines()]
                results = [e["ok"] for e in trace if e["kind"] == "tool_result" and e["tool"] == "submit_review"]
                self.assertEqual(results, [True] if preserved else [False, False, True])
                if not preserved:
                    report, = [e["handoff"] for e in detail["room_events"] if e.get("role") == "inspector"
                               and e["kind"] == "member_report"]
                    history, = report["native_review"]["historical_execution_receipts"]
                    signed, = report["native_review"]["assessment"]["results"][0]["receipts"]
                    self.assertEqual(history["scope"], signed["scope"])
                    self.assertEqual(history["paths"], [mutation])
                    self.assertNotEqual(history["ref"], signed["ref"])
                    self.assertNotEqual(history["tool_call_id"], signed["tool_call_id"])
                    self.assertIn(signed["ref"], str(provider.requests[-1]))
                if mutation == report_path and not preserved:
                    self.assertIn("报告已更新", str(provider.requests))
                    self.assertIn("无需回写报告", str(provider.requests))

    def test_reviewer_cannot_rewrite_its_own_earlier_report_when_bound_as_original_reference(self):
        ref = "original-dispatch.txt"
        manuscript = "Shipment destination: old warehouse. Verified dispatch record."
        project = self.root / "project"
        project.mkdir()
        steps = [tool("delegate_task", assignee="inspector", objective="Publish the confirmed dispatch record.",
                      remaining_roles=["builder"], deliverable_paths=[ref]),
                 write(ref, manuscript), {"text": "Record archived."}, *self.start_review()]
        assignment = steps[-2]["tool_calls"][0]["arguments"]
        assignment["deliverable_paths"] = [ref]
        assignment["review"]["criteria"] = [{**RULE, "reference_path": ref}]
        approval = submit(manuscript, "passed")
        approval["tool_calls"][0]["arguments"]["results"][0]["evidence"].append("reference:" + ref)
        steps += [write(ref, "Changed dispatch reference."), tool("read_file", path=ref), approval,
                  {"text": "[BLOCKED] The requested output is also protected reference evidence."},
                  {"text": "[BLOCKED] Independent input must remain unchanged."}]
        detail, provider = self.execute(steps, workspace_path=str(project),
                                       objective="@lead Please obtain the original dispatch, prepare the label and verify it.")
        self.assertEqual(detail["run"]["status"], "failed", detail["diagnostics"])
        self.assertIn("独立评审的冻结文件不可修改", str(provider.requests))
        self.assertEqual((project / ref).read_text(), manuscript)
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        refs = reports[-1]["native_review"]["assessment"]["results"][0]["receipts"]
        self.assertEqual([r["sha256"] for r in refs if r["kind"] == "reference"], [hashlib.sha256(manuscript.encode()).hexdigest()])

    def reviewed_label(self):
        label = "Shipment destination: old warehouse. Verified dispatch record."
        script = self.start_review()
        # A real stale delegation error preceded the accepted review.
        script.insert(3, tool("delegate_task", assignee="inspector", objective="Verify the label.",
                              remaining_roles=[], review={"candidate": "unknown-target", "criteria": [RULE]}))
        # The signed review is handed off by the host; the next scripted
        # response belongs to the coordinator, including genuine blockers.
        return script + [submit(label, "passed")]

    def close_events(self, detail):
        trace = self.root / "runs" / detail["run"]["id"] / "trace.jsonl"
        return [row for line in trace.read_text().splitlines()
                if (row := json.loads(line))["kind"] == "reviewed_delivery_blocked"]

    def test_review_file_tools_refuse_frozen_inputs_but_allow_a_new_note(self):
        label = "Shipment destination: old warehouse. Verified dispatch record."
        project = self.root / "project"
        project.mkdir()
        inherited = project / "old_check.py"
        inherited.write_text("print('original')\n")
        # The auxiliary check existed before the author wrote the candidate.
        # The review must protect both paths while still accepting fresh work.
        attempted = [
            write("sub/../old_check.py", "modified"),
            tool("edit_file", path=PATH, old_text="warehouse", new_text="station"),
            tool("append_file", path="old_check.py", content="extra"),
            tool("delete_file", path=PATH),
        ]
        batch = {"tool_calls":[step["tool_calls"][0] for step in attempted]}
        steps = self.start_review() + [batch, write("independent-note.md", "Checked the original label."),
            submit(label, "passed"), {"text":"Reviewed original materials."}, {"text":"Delivered label."}]
        detail, provider = self.execute(steps, workspace_path=str(project))
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertEqual(inherited.read_text(), "print('original')\n")
        self.assertEqual((project / PATH).read_text(), label)
        self.assertEqual((project / "independent-note.md").read_text(), "Checked the original label.")
        results = [m for m in provider.requests[6][0] if m.get("role") == "tool"
                   and "独立评审的冻结文件不可修改" in m.get("content", "")]
        self.assertEqual(len(results), 4)
        self.assertIn(hashlib.sha256(inherited.read_bytes()).hexdigest(), results[0]["content"])
        self.assertIn("系统临时目录", results[0]["content"])

    def test_command_change_to_inherited_check_is_reported_before_submit_and_blocks_handoff(self):
        label = "Shipment destination: old warehouse. Verified dispatch record."
        project = self.root / "project"
        project.mkdir()
        inherited = project / "old_check.py"
        inherited.write_text("print('original')\n")
        altered = "print('changed')"
        command = f"from pathlib import Path; Path('old_check.py').write_text({altered!r})"
        steps = self.start_review() + [tool("run_python", code=command), submit(label, "passed"),
            {"text":"[BLOCKED] Need a fresh frozen review copy."}, {"text":"[BLOCKED] Review pending."}]
        detail, provider = self.execute(steps, workspace_path=str(project))
        self.assertEqual(detail["run"]["status"], "failed")
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertFalse(reports[-1]["published"])
        self.assertIsNone(reports[-1]["native_review"]["assessment"])
        self.assertEqual(inherited.read_text(), "print('original')\n")
        before_submit = str(provider.requests[6][0])
        refusal = next(m["content"] for m in provider.requests[7][0]
                       if m.get("role") == "tool" and "评审快照与绑定版本" in m.get("content", ""))
        for feedback in (before_submit, refusal):
            self.assertIn("old_check.py", feedback)
            self.assertIn(hashlib.sha256(inherited.read_bytes()).hexdigest(), feedback)
            self.assertIn(hashlib.sha256(altered.encode()).hexdigest(), feedback)
            self.assertIn(version(label), feedback)
            self.assertIn("从原冻结版本重新发起评审", feedback)

    def test_repeated_identical_review_checks_recover_request_with_evidence_and_all_work_tools(self):
        label = "Shipment destination: old warehouse. Verified dispatch record."
        scope = ("from pathlib import Path\ntext = Path('delivery.txt').read_text()\n"
                 "assert 'old warehouse' in text\nprint('PASS destination matches the dispatch')")
        check = tool("run_python", code=scope)
        new_check = tool("run_python", code=scope.replace("'old warehouse' in text", "text.endswith('record.')"))
        note = "Verified the warehouse. Separately check the dispatch record suffix."
        record_note = {"tool_calls": [write("review-note.md", note)["tool_calls"][0],
                                      tool("read_file", path="review-note.md")["tool_calls"][0]]}
        steps = self.start_review() + [check] * 7 + [record_note,
            new_check, submit(label, "passed"), {"text":"Delivered."}]
        detail, provider = self.execute(steps)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        requests = provider.requests
        evidence_turn = len(self.start_review()) + 7
        normal, _ = requests[len(self.start_review()) + 2]
        self.assertIn("进度提醒", normal[-1]["content"])  # Delivered before any compact request.
        self.assertTrue(any(m["role"] == "tool" for m in normal))
        prompt, tools = requests[evidence_turn]
        self.assertEqual(tools, requests[len(self.start_review())][1])
        self.assertFalse(any(m["role"] in {"tool", "assistant"} for m in prompt))
        first_review_request = requests[len(self.start_review()) - 1][0]
        self.assertEqual(prompt[:3], first_review_request[:3])  # System, assignment context, bound review goal.
        contents = "\n".join(m["content"] for m in prompt)
        for fact in ("工作上下文恢复", "7 次", "Build and independently verify the shipment label.",
                     PATH, RULE["description"], version(label),
                     "PASS destination matches the dispatch", "recent_evidence", "passed_check", '"needs":{}',
                     "list_review_evidence", "submit_review"):
            self.assertIn(fact, contents)
        self.assertIn("write_file", tools)
        self.assertIn("run_python", tools)
        self.assertNotIn(scope, contents)
        owner, = [r["task_id"] for r in detail["room_events"] if r["kind"] == "assignment"
                  and r["recipient"] == "inspector"]
        trail = [r for r in detail["events"] if r.get("task_id") == owner]
        self.assertEqual(sum(r["kind"] == "tool_result" and r["tool"] == "run_python" for r in trail), 8)
        compact = [r for r in trail if r["kind"] == "step_start" and r.get("compact")]
        self.assertTrue(compact)
        self.assertTrue(all(r["phase"] == "work" and set(r["offered_tools"]) == set(tools) for r in compact))
        # The next decision sees both new receipts and their actual calls,
        # without reintroducing the seven pre-checkpoint copies of the test.
        resumed, offered = requests[evidence_turn + 1]
        self.assertEqual(resumed[:len(prompt)], prompt)
        appended = resumed[len(prompt):]
        assistant, first, second = appended[:3]
        calls = assistant["tool_calls"]
        self.assertEqual([call["function"]["name"] for call in calls], ["write_file", "read_file"])
        self.assertEqual([first["tool_call_id"], second["tool_call_id"]], [call["id"] for call in calls])
        self.assertIn(note, second["content"])
        self.assertEqual(offered, tools)
        self.assertNotIn(scope, str(resumed))
        next_step = next(r for r in trail if r["kind"] == "step_start" and r["step"] == compact[-1]["step"] + 1)
        self.assertEqual(next_step["work_history_start"], compact[-1]["work_history_start"])
        self.assertEqual(next_step["work_history_tail_messages"], 3)
        report, = [r["handoff"] for r in detail["room_events"] if r["kind"] == "member_report"
                   and r["role"] == "inspector"]
        self.assertTrue(report["published"])
        self.assertEqual(report["native_review"]["state"], "passed")
        self.assertIn("review-note.md", [f["path"] for f in report["changed_files"]])

    def test_same_batch_command_after_review_submission_invalidates_its_handoff(self):
        label = "Shipment destination: old warehouse. Verified dispatch record."
        command = tool("run_python", code=f"from pathlib import Path; Path({PATH!r}).write_text('changed')")
        batch = {"tool_calls":[submit(label, "passed")["tool_calls"][0], command["tool_calls"][0]]}
        detail, _ = self.execute(self.start_review() + [batch, {"text":"Reviewed the label."},
                                 {"text":"[BLOCKED] Review copy drifted."}])
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual(detail["run"]["status"], "failed")
        self.assertFalse(reports[-1]["published"])
        self.assertIsNone(reports[-1]["native_review"]["assessment"])
        self.assertIn("验收提交后候选文件已变化", reports[-1]["remaining_issue"])
        self.assertIn(PATH, reports[-1]["remaining_issue"])
        self.assertIn(hashlib.sha256(label.encode()).hexdigest(), reports[-1]["remaining_issue"])

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

    def test_revised_reviewer_probe_with_diagnostics_hands_off_to_an_independent_peer(self):
        label = "Shipment destination: old warehouse. Verified dispatch record."
        probe = """from pathlib import Path
import sys
errors = []
def record(name, condition, observed):
    print(("PASS  " if condition else "FAIL  ") + name + "  | " + str(observed))
    if not condition:
        errors.append(name)
label = Path("delivery.txt").read_text()
length = len(label) + 10
record("shipment label length", length == EXPECTED, length)
record("warehouse present", "old warehouse" in label, "found warehouse")
sys.exit(bool(errors))
""".replace("EXPECTED", str(len(label)))
        fixed = probe.replace("len(label) + 10", "len(label)")
        command = "python3 label_probe.py"
        checked = "pass:" + hashlib.sha256((command + "\0").encode()).hexdigest()
        second = {"candidate":"inspector", "criteria":[{"id":"probe", "description":"Independently verify the corrected length calculation.", "method":"execute"}]}
        steps = self.start_review() + [write("label_probe.py", probe), tool("run_shell", command=command),
            tool("edit_file", path="label_probe.py", old_text="len(label) + 10", new_text="len(label)"),
            tool("run_shell", command=command), submit(label, "passed"), {"text":"Reviewed the original label and fixed my test."},
            tool("delegate_task", assignee="builder", objective="Independently verify the corrected calculation.", review=second),
            tool("read_file", path="label_probe.py"), tool("run_shell", command=command),
            tool("submit_review", candidate_version=digest({"label_probe.py":hashlib.sha256(fixed.encode()).hexdigest()}),
                 results=[{"criterion_id":"probe", "verdict":"passed","reason":"Checked the expected length against the label and reran both assertions.",
                           "evidence":[checked]}]),
            {"text":"Verified the corrected probe independently."}, {"text":"Shipment handed over."}]
        detail, _ = self.execute(steps)
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual(detail["run"]["status"], "completed", [(r["role"], r["remaining_issue"]) for r in reports])
        first, peer = reports[-2:]
        self.assertTrue(first["published"])
        self.assertEqual(first["native_review"]["state"], "passed")
        retired, = first["evidence"]["superseded_member_checks"]
        self.assertIn("pending_independent_acceptance", retired["disposition"])
        self.assertEqual((retired["prior_exit_code"], retired["current_exit_code"]), (1, 0))
        self.assertIn("shipment label length", retired["prior_failure"])
        self.assertEqual(peer["independently_reviewed_tasks"], [first["task_id"]])

    def test_host_routes_signed_revision_and_recheck_when_lead_rereads_or_stops(self):
        old = "Shipment destination: old warehouse. Verified dispatch record."
        new = "Shipment destination: new warehouse. Verified dispatch record."
        script = self.start_review() + [
            submit(old, "changes_requested", cause="product", action="Print the confirmed new warehouse."),
            {"text": "The submitted assessment lists the correction."},
            tool("read_file", path=PATH),
            write(PATH, new), {"text": "Fixed the label."},
            {"text": "[BLOCKED] I cannot independently sign my team's label."},
            tool("read_file", path=PATH), submit(new, "passed"),
            {"text": "Checked the corrected candidate."}, {"text": "Here is the reviewed label."}]
        detail, _ = self.execute(script)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        transfers = [e for e in detail["events"] if e["kind"] == "bound_handoff_dispatched"]
        self.assertEqual([(e["state"], e["owner"], e["version"]) for e in transfers],
                         [("repair", "builder", version(old)), ("review", "inspector", version(new))])
        self.assertEqual(transfers[0]["target"], transfers[1]["target"])
        calls = {e.get("tool_call_id") for e in detail["events"]
                 if e["kind"] == "tool_start" and e.get("tool") == "delegate_task"}
        self.assertTrue(all(e["call_id"] in calls for e in transfers))

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
        self.assertTrue(any("原始端点独立复算" in str(messages) and "submit_review" in str(tools)
                            for messages, tools in provider.requests))
        report = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"][-1]
        receipts = report["native_review"]["assessment"]["results"][0]["receipts"]
        evidence = next(r for r in receipts if r["kind"] == "source")
        self.assertEqual(evidence["url"], url)
        self.assertEqual(evidence["observed_bytes_sha256"], hashlib.sha256(article.encode()).hexdigest())

    def test_a_local_reference_receipt_cannot_vouch_for_an_external_claim(self):
        label = "Shipment destination: old warehouse. Verified dispatch record."
        url = "https://issuer.example/dispatch"
        project = self.root / "project"
        project.mkdir()
        (project / "dispatch.txt").write_text(label)
        local = {**RULE, "reference_path":"dispatch.txt"}
        external = {**RULE, "id":"external", "method":"source"}
        script = self.start_review()
        script[3] = tool("delegate_task", assignee="inspector", objective="Compare the local order and verify the issuer.",
                         review={"candidate":"builder", "criteria":[local, external]})
        locally_checked = {"criterion_id":"content", "verdict":"passed", "reason":"Local order matches.",
                           "evidence":["file:" + PATH, "reference:dispatch.txt"]}
        external_claim = {**locally_checked, "criterion_id":"external"}
        def decide(claim):
            return tool("submit_review", candidate_version=version(label), results=[locally_checked, claim])
        steps = script + [tool("read_file", path="dispatch.txt"), decide(external_claim),
                          tool("http_get", url=url), decide({**external_claim, "evidence":["file:" + PATH, "source:" + url]}),
                          {"text":"Checked both records."}, {"text":"Delivered."}]
        with patch("miniagent.research.fetch", return_value=Page(url, "text/plain", label.encode(), False)):
            detail, provider = self.execute(steps, workspace_path=str(project))
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertTrue(any("本次亲自打开的来源" in str(messages) for messages, _ in provider.requests))
        report = next(e["handoff"] for e in detail["room_events"]
                      if e["kind"] == "member_report" and e["handoff"]["role"] == "inspector")
        rows = report["native_review"]["assessment"]["results"]
        self.assertEqual([[e["kind"] for e in r["receipts"]] for r in rows], [["read","reference"], ["read","source"]])

    def test_local_comparison_requires_independent_reads_of_candidate_and_named_reference(self):
        label = "Shipment destination: old warehouse. Verified dispatch record."
        project = self.root / "project"
        project.mkdir()
        record = project / "dispatch.txt"
        record.write_text("Confirmed destination: old warehouse.\n")
        (project / "other.txt").write_text("A different record.\n")
        script = self.start_review()
        def inspect(method, path):
            return tool("delegate_task", assignee="inspector", objective="Check the label against the dispatch record.",
                        review={"candidate":"builder", "criteria":[{**RULE, "method":method, "reference_path":path}]})
        script[3:4] = [inspect("source", record.name), inspect("inspect", "missing.txt"), inspect("inspect", record.name)]
        both = submit(label, "passed")
        both["tool_calls"][0]["arguments"]["results"][0]["evidence"].append("reference:" + record.name)
        detail, provider = self.execute(script + [
            tool("read_file", path="other.txt"), submit(label, "passed"),
            tool("read_lines", path=record.name, start_line=1), both,
            {"text":"Checked the original record."}, {"text":"Delivered."}], workspace_path=str(project))
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        results = [str(messages) for messages, _ in provider.requests]
        self.assertTrue(any("本地原件对照请使用 method=inspect" in result for result in results))
        self.assertTrue(any("找不到本地原始记录 missing.txt" in result for result in results))
        self.assertTrue(any("须读取并同时引用" in result for result in results))
        report = next(e["handoff"] for e in detail["room_events"]
                      if e["kind"] == "member_report" and e["handoff"]["role"] == "inspector")
        assessment = report["native_review"]["assessment"]
        receipts = assessment["results"][0]["receipts"]
        self.assertEqual([(r["kind"], r["path"]) for r in receipts], [("read", PATH), ("reference", record.name)])
        self.assertEqual(receipts[1]["sha256"], hashlib.sha256(record.read_bytes()).hexdigest())

        # The approved candidate remains byte-identical while another member
        # publishes an amended original record. Its old comparison must reopen.
        queue = ReviewQueue()
        item = Review("build", "builder", "inspector", "Verify the label against its record.",
                      [Rule.model_validate({**RULE, "reference_path":record.name})], {PATH:receipts[0]["sha256"]})
        item.apply(assessment, "")
        queue.items[item.target] = item
        queue.published({"task_id":"update", "role":"lead", "deliverable_paths":[record.name]},
                        {**item.files, record.name:"new-original-sha"}, [{"path":record.name, "sha256":"new-original-sha"}])
        self.assertEqual(item.state, "review")

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
        final = (self.root / "runs" / detail["run"]["id"] / "conversation-answer.txt").read_text()
        self.assertIn("签署 1 项，其中 0 项判为通过", final)
        self.assertIn("1 条不同回执", final)
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

    def test_autonomous_review_closes_with_signed_gap_when_private_authority_is_required(self):
        manuscript = "Shipment destination: old warehouse. Verified dispatch record."
        missing = submit(manuscript, "inconclusive", cause="business",
                         action="Only the owner can authorize the shipment destination.")
        detail, provider = self.execute(
            self.start_review() + [missing, {"text": "Owner authorization is unavailable."},
                                   {"text": "[BLOCKED] Cannot certify destination without owner authorization."}],
            interaction_mode="autonomous")
        self.assertEqual(detail["run"]["status"], "failed", detail["run"])
        self.assertFalse(detail["pending_questions"])
        self.assertFalse({event["kind"] for event in detail["room_events"]} & {"human_question", "human_answer"})
        review, = [event["handoff"]["native_review"] for event in detail["room_events"]
                   if "native_review" in event.get("handoff", {})]
        self.assertEqual(review["state"], "blocked")
        self.assertEqual(review["findings"][0]["cause"], "business")
        self.assertIn("authorize the shipment", detail["conversation"]["turns"][-1]["answer"])
        self.assertFalse(provider.requests[-1][1])

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
