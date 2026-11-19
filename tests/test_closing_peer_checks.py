import hashlib
import unittest
from unittest.mock import Mock, patch

from miniagent.check_provenance import shell_invocation
from miniagent.config import Settings
from miniagent.peer_recheck import ClosingPeerChecks
from miniagent.schemas import ToolResult
from miniagent.studio_reviews import digest
from miniagent.studio_service import StudioService
from miniagent.tools import Tool, ToolContext, ToolRegistry
from miniagent.sandbox import Sandbox
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


class ClosingPeerChecksTest(unittest.TestCase):
    def test_exact_scope_and_version_checked_at_offer_and_execution(self):
        directory = make_temp_dir("recheck-scope")
        self.addCleanup(cleanup, directory)
        sandbox = Sandbox(directory)
        ctx = ToolContext(sandbox=sandbox, limits=Settings().limits)
        original_fn = Mock(return_value=ToolResult.success("ran"))
        original = Tool("run_shell", "shell", {"type": "object", "properties": {
            "command": {"type": "string"}, "cwd": {"type": "string"}}, "required": ["command"]}, original_fn)
        registry = ToolRegistry([original])
        current = {"product.txt": "v1", "checks/test.py": "a1"}
        bound = ClosingPeerChecks(registry, directory, current.copy)
        check = shell_invocation("python3 test.py", "checks")
        item = {"task": "author", "candidate": dict(current), "verification": [
            {"scope": check.scope, "check": check.caption, "path": check.script, "current_sha256": "a1"}]}
        evidence = {"complete_verification": {}, "script_exits": {}}
        frozen = dict(current)
        args = {"command": "python3 test.py", "cwd": "checks"}

        self.assertIsNone(bound.offer([], "author", frozen, evidence))
        self.assertIsNone(bound.offer([item], "different task", frozen, evidence))
        self.assertIsNone(bound.offer([item], "author", {**frozen, "product.txt": "v2"}, evidence))
        self.assertIsNone(bound.offer([item], "author", frozen, {"complete_verification": {},
            "script_exits": {check.scope: {"path": check.script, "sha256": "a1", "exit_code": 0}}}))
        self.assertIsNotNone(bound.offer([item], "author", frozen, evidence))
        for invalid in ({**args, "command": "python3 test.py extra"}, {**args, "cwd": "."}, {**args, "yield_ms": 1}):
            self.assertFalse(registry.dispatch("run_shell", invalid, ctx).ok)
        current["product.txt"] = "changed after tool was offered"
        self.assertFalse(registry.dispatch("run_shell", args, ctx).ok)
        original_fn.assert_not_called()
        current["product.txt"] = "v1"
        self.assertTrue(registry.dispatch("run_shell", args, ctx).ok)
        self.assertFalse(registry.dispatch("run_shell", args, ctx).ok)
        original_fn.assert_called_once_with(ctx, args)
        bound.restore_shell()
        self.assertIs(registry.get("run_shell"), original)
        self.assertIsNone(bound.offer([item], "author", frozen, evidence))

        # An old digest or a filtered invocation cannot become a new capability.
        current["checks/test.py"] = "a2"
        self.assertIsNone(bound.offer([item], "author", frozen, evidence))
        changed = dict(current)
        next_entry = {**item["verification"][0], "current_sha256": "a2"}
        changed_item = {"task": "author", "candidate": changed, "verification": [next_entry]}
        for bad in ("python3 test.py | head", "python3 test.py --select=one", "node test.py (cwd=checks)"):
            self.assertIsNone(bound.offer([{**changed_item, "verification": [{**next_entry, "check": bad}]}],
                                          "author", changed, evidence))
        self.assertIsNotNone(bound.offer([changed_item], "author", changed, evidence))

    def reviewed_label(self, closing_command):
        root = make_temp_dir("close-peer")
        self.addCleanup(cleanup, root)
        service = StudioService(root / "runs", Settings(api_key="test-only"))
        self.addCleanup(service.close)
        label = "Central warehouse on Market Street is the confirmed shipment destination."
        old = "import sys\nsys.tracebacklimit = 0\nassert 'wrong' in open('label.txt').read()\nprint('1 passed')\n"
        new = old.replace("wrong", "Central")
        candidate = digest({path: hashlib.sha256(data.encode()).hexdigest() for path, data in
                            [('label.txt', label), ('check.py', new), ('check.log', 'AssertionError\n')]})
        rule = {"id":"label", "method":"inspect", "description":"Read the warehouse label."}
        command = "python3 check.py"
        script = [
            tool("delegate_task", assignee="author", objective="Deliver the label", remaining_roles=["qa"],
                 deliverable_paths=["label.txt", "check.py"]),
            write("label.txt", label), write("check.py", old),
            tool("run_shell", command='python3 check.py > check.log 2>&1; echo "EXIT=$?"; tail -3 check.log'),
            tool("edit_file", path="check.py", old_text="wrong", new_text="Central"),
            tool("run_shell", command=command), {"text":"Label ready; the revised checker needs a peer."},
            tool("delegate_task", assignee="qa", objective="Review the label and write review.md",
                 deliverable_paths=["review.md"], remaining_roles=[], review={"candidate":"author", "criteria":[rule]}),
            tool("read_file", path="label.txt"), tool("run_shell", command=command),
            write("review.md", "I inspected the current warehouse label."),
            tool("submit_review", candidate_version=candidate, results=[{"criterion_id":"label", "verdict":"passed",
                 "reason":"Read label.txt and confirmed the destination.", "evidence":["file:label.txt"]}]),
            tool("run_shell", command=closing_command), {"text":"Read the verification response."}, {"text":"Delivered the label."}
        ]
        provider = RecordingProvider(script)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = service.start(request("team", conversation=roster("lead", "author", "qa"),
                                        objective="Deliver a reviewed warehouse label", acceptance_files=["label.txt"],
                                        limits={"max_steps":90}))
            detail = finished(service, run["id"])
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"]=="member_report"]
        return root, detail, provider, reports

    def test_ordinary_review_reruns_an_author_check_invalidated_by_new_notes(self):
        _, detail, provider, reports = self.reviewed_label("python3 check.py")
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertEqual(provider.requests[12][1], ["run_shell"])
        self.assertIn("收尾前", str(provider.requests[12][0]))
        author, qa = reports
        self.assertTrue(author["evidence"]["pending_verification"])
        self.assertEqual(qa["independently_reviewed_tasks"], [author["task_id"]])


    def test_different_command_in_closing_window_cannot_execute_or_close_the_authors_check(self):
        root, detail, provider, reports = self.reviewed_label("touch unexpected.md")
        self.assertEqual(provider.requests[12][1], ["run_shell"])
        self.assertEqual(detail["run"]["status"], "failed")
        self.assertTrue(reports[0]["evidence"]["pending_verification"])
        self.assertEqual(reports[1]["independently_reviewed_tasks"], [])
        self.assertFalse(any(root.rglob("unexpected.md")))
