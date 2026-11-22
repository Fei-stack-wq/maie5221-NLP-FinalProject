"""A worker requests an independent read of its private draft through normal Studio tools."""
import hashlib
import json
import unittest
from collections import deque
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.studio_reviews import digest
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


class AuthorReviewRequestTest(unittest.TestCase):
    def test_complete_draft_is_frozen_for_selected_peer(self):
        root = make_temp_dir("author-review")
        self.addCleanup(cleanup, root)
        workspace = root / "project"
        workspace.mkdir()
        service = StudioService(root / "runs", Settings(api_key="test-credential"))
        self.addCleanup(service.close)
        goal = ('Create a “booking guide” that names room A101 and lists reception as the check-in contact.\n'
                "Then  publish notes.")
        requested_checks = [{"kind": "contains", "path": "RELEASE_NOTES.md", "text": "A101"}]
        content = "Booking instructions for room A101. For check-in, contact reception."
        version = digest({"guide.txt": hashlib.sha256(content.encode()).hexdigest()})
        release = deque([write("RELEASE_NOTES.md", "Guide ready for room A101."), {"text": "Release confirmed."}])
        author = deque([
            tool("request_peer_review", reason="Check my draft.", reviewer="qa"),
            write("guide.txt", content),
            tool("request_peer_review", reason="Check my own draft.", reviewer="dev"),
            tool("request_peer_review", reason="Independently verify the instructions."),
        ])
        requirements = [
            {"id": "room", "user_quote": "names room A101", "description": "The booking guide identifies room A101.",
             "method": "inspect", "artifact_paths": ["guide.txt"]},
            {"id": "contact", "user_quote": "lists reception as the check-in contact",
             "description": "The check-in contact in the guide is reception.",
             "method": "inspect", "artifact_paths": ["guide.txt"]},
        ]
        findings = [
            {"criterion_id": "room", "verdict": "passed", "reason": "The guide names room A101.",
             "evidence": ["file:guide.txt"]},
            {"criterion_id": "contact", "verdict": "passed", "reason": "The guide directs visitors to reception.",
             "evidence": ["file:guide.txt"]},
        ]
        review = deque([
            tool("read_file", path="guide.txt"),
            tool("submit_review", candidate_version=version, results=[
                {"criterion_id": "independent_recheck", "verdict": "passed",
                 "reason": "I read the frozen guide.", "evidence": ["file:guide.txt"]}]),
            tool("register_review_criteria", candidate_version=version, criteria=requirements),
            tool("submit_review", candidate_version=version, results=findings[:1]),
            tool("submit_review", candidate_version=version, results=findings),
        ])
        reviewer_inputs = []

        class Provider(ScriptedProvider):
            def chat(inner, messages, tools=None, temperature=0):
                names = {item.name for item in tools or ()}
                member = messages[0]["content"]
                if not inner.cursor:
                    reply = tool("delegate_task", assignee="dev", objective="Write the guide; QA checks it.",
                                 remaining_roles=["release", "qa"], deliverable_paths=["guide.txt"])
                elif "「dev」" in member:
                    reply = author.popleft() if author else {"text": "[BLOCKED] Review requested."}
                elif "「qa」" in member:
                    self.assertIn("Independently verify the instructions.", str(messages))
                    if not reviewer_inputs:
                        reviewer_inputs.append(("\n".join(m.get("content") or "" for m in messages),
                                                [spec.to_api() for spec in tools]))
                    self.assertIn("register_review_criteria", names)
                    self.assertTrue(review, "The signed frozen review must finish without another model reply.")
                    reply = review.popleft()
                elif "delegate_task" in names and review:
                    reply = tool("delegate_task", assignee="qa", objective="Review the frozen guide.")
                elif "delegate_task" in names:
                    reply = tool("delegate_task", assignee="release", objective="Write the release notes.",
                                 deliverable_paths=["RELEASE_NOTES.md"])
                else:
                    reply = {"text": "Delivered."}
                if "「release」" in member:
                    reply = release.popleft()
                inner.script.append(reply)
                return super().chat(messages, tools, temperature)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=Provider([])):
            run = service.start(request("team", conversation=roster("lead", "dev", "release", "qa"),
                                        objective=goal,
                                        acceptance_files=["guide.txt", "RELEASE_NOTES.md"],
                                        acceptance_checks=requested_checks,
                                        workspace_path=str(workspace)))
            detail = finished(service, run["id"])
        run_dir = root / "runs" / run["id"]
        self.assertEqual(json.loads((run_dir / "studio.json").read_text())["request"]["objective"], goal)
        self.assertEqual([e["text"] for e in detail["room_events"] if e["kind"] == "goal"], [goal])
        (reviewer_prompt, advertised_tools), = reviewer_inputs
        context = reviewer_prompt.split("当前分工资料：", 1)[1]
        facts, _ = json.JSONDecoder().raw_decode(context)
        self.assertEqual(facts["group_goal"], goal)
        self.assertEqual(facts["team_checks"]["checks"], requested_checks)
        self.assertNotIn("criteria_registration", facts)
        model_input = reviewer_prompt + json.dumps(advertised_tools, ensure_ascii=False)
        # Previously injected through three separate channels: context, tool help and field schema.
        for former_hint in ("数值、阈值和边界", "计算、边界及硬上限", "执行不同动作的边界例子"):
            self.assertNotIn(former_hint, model_input)
        for definition in requirements:
            self.assertNotIn(definition["description"], model_input)
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual([r["role"] for r in reports[:2]], ["dev", "qa"], reports)
        self.assertFalse(reports[0]["published"])
        pending = reports[0]["author_probe_review"]
        self.assertEqual((pending["reviewer"], pending["candidate_version"]), ("qa", version))
        self.assertEqual(pending["request"]["source"], "request_peer_review")
        self.assertEqual(reports[1]["native_review"]["state"], "passed")
        self.assertEqual(reports[1]["native_review"]["acceptance_contract"]["definitions"], requirements)
        self.assertEqual(reports[1]["signed_handoff"]["assessment"]["verdict"], "passed")
        self.assertEqual(reports[1]["promoted_candidate"]["source_task"], reports[0]["task_id"])
        self.assertEqual((workspace / "guide.txt").read_text(), content)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertTrue(reports[2]["published"])
        self.assertIn("A101", (workspace / "RELEASE_NOTES.md").read_text())
        trace = run_dir / "trace.jsonl"
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        def results(name):
            return [e["ok"] for e in events if e["kind"] == "tool_result" and e["tool"] == name]
        self.assertEqual(results("request_peer_review"), [False, False, True])
        self.assertEqual(results("register_review_criteria"), [True])
        self.assertEqual(results("submit_review"), [False, False, True])
        signed = reports[1]["signed_handoff"]["assessment"]["results"]
        self.assertEqual([row["criterion_id"] for row in signed], ["room", "contact"])

    def test_unrelated_green_checks_cannot_discharge_old_failures_or_release_an_invalid_candidate(self):
        from miniagent.crew_progress import _check_key, _script_inputs
        code = "def book():\n    return 'A101'\n"
        check = "import runpy\nassert runpy.run_path('booking.py')['book']() == 'A101'\nprint('PASS booking')"
        code_version = hashlib.sha256(code.encode()).hexdigest()
        for scenario in ("code", "unbound_shell", "root_requirement"):
            with self.subTest(scenario=scenario):
                root = make_temp_dir("review-guards")
                self.addCleanup(cleanup, root)
                workspace = root / "project"
                workspace.mkdir()
                service = StudioService(root / "runs", Settings(api_key="test-credential"))
                self.addCleanup(service.close)
                author = [write("booking.py", code)]
                manifest = {"booking.py": code_version}
                if scenario == "unbound_shell":
                    unpassed = "import unittest\nclass ExportTest(unittest.TestCase):\n    def test_export(self):\n        self.fail('unresolved export failure')\n"
                    (workspace / "test_export.py").write_text(unpassed)
                    manifest["test_export.py"] = hashlib.sha256(unpassed.encode()).hexdigest()
                    author.append(tool("run_shell", command="python3 -m unittest test_export"))
                author.append(tool("request_peer_review", reason="Verify the booking behavior."))
                def sign(ref):
                    return tool("submit_review", candidate_version=digest(manifest), results=[
                        {"criterion_id": "independent_recheck", "verdict": "passed", "reason": "Observed book().",
                         "evidence": [ref]}])
                reviewer = deque([tool("read_file", path="booking.py"), sign("file:booking.py"),
                                  tool("run_python", code=check), sign("pass:" + _check_key(_script_inputs(check)[1]))])
                author = deque(author)
                class Provider(ScriptedProvider):
                    def chat(inner, messages, tools=None, temperature=0):
                        names = {spec.name for spec in tools or ()}
                        actor = messages[0]["content"]
                        if not inner.cursor:
                            reply = tool("delegate_task", assignee="dev", objective="Implement book()",
                                         remaining_roles=["qa"], deliverable_paths=["booking.py"])
                        elif "「dev」" in actor:
                            reply = author.popleft() if author else {"text": "[BLOCKED] Current candidate remains incomplete."}
                        elif "「qa」" in actor:
                            reply = reviewer.popleft() if reviewer else {"text":
                                "[BLOCKED] Open verification gap." if scenario == "unbound_shell" else "Reviewed."}
                        elif "delegate_task" in names and reviewer:
                            reply = tool("delegate_task", assignee="qa", objective="Review the booking candidate.")
                        else:
                            reply = {"text": "Delivered." if scenario == "code" else "[BLOCKED] Verification incomplete."}
                        inner.script.append(reply)
                        return super().chat(messages, tools, temperature)
                checks = [{"kind":"contains","path":"booking.py","text":"room C303"}] if scenario == "root_requirement" else []
                with patch("miniagent.studio_service.DeepSeekProvider", return_value=Provider([])):
                    run = service.start(request("team", conversation=roster("lead", "dev", "qa"),
                        objective="Implement book() and verify the current behavior.", workspace_path=str(workspace),
                        acceptance_files=["booking.py"], acceptance_checks=checks, limits={"max_steps": 45}))
                    detail = finished(service,run["id"])
                reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
                events = [json.loads(line) for line in (root/"runs"/run["id"]/"trace.jsonl").read_text().splitlines()]
                submissions = [e for e in events if e["kind"] == "tool_result" and e["tool"] == "submit_review"]
                self.assertEqual([e["ok"] for e in submissions], [False, scenario != "unbound_shell"], submissions)
                self.assertEqual(reports[1]["native_review"]["criteria"][0]["method"], "execute")
                self.assertEqual((workspace/"booking.py").exists(), scenario == "code", reports)
                if scenario == "unbound_shell":
                    self.assertTrue(reports[0]["author_probe_review"]["unbound_failures"])
                    self.assertTrue(reports[1]["evidence"]["observed_failure"])
                if scenario == "root_requirement":
                    self.assertIn("用户指定验收失败", reports[1]["remaining_issue"])
