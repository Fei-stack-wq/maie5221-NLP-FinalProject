"""Replay a filtered failing test and unchanged edits through the normal DM entry point."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from miniagent.acceptance import bind, missing_file_probe
from miniagent.config import Settings
from miniagent.metrics import load_events
from miniagent.studio_acceptance import build_checks
from miniagent.studio_service import StudioService
from tests.test_studio_conversations import RecordingProvider, fresh, msg, tool
from tests.test_studio_service import finished


class WorkerWithFallback(RecordingProvider):
    """Change plans only when the workspace, failure and missing handoff are reported together."""
    def __init__(self, initial, retry, repair):
        super().__init__([*initial, *retry * 6])
        self.repair = repair

    def chat(self, messages, tools=None, temperature=0):
        evidence = ("restores previous state", "config.txt", "handoff.md")
        if self.repair and any(msg["role"] == "user" and all(s in msg["content"] for s in evidence)
                               for msg in messages):
            self.script[self.cursor:] = self.repair
            self.repair = []
        return super().chat(messages, tools, temperature)


class StudioLoopFeedbackTest(unittest.TestCase):
    def test_scripted_worker_can_change_strategy_after_a_filtered_failure(self):
        # The runner exits 7 until the actual configuration is repaired.
        check = "grep -qx fixed config.txt && exit 0\nprintf '✗ restores previous state\\n'\nexit 7\n"
        command = "sh check.sh 2>&1 | sed -n '/state/p'; sh check.sh 2>&1 | tail -2; printf 'done\\n'"
        repeat = [
            tool("run_shell", command=command),
            tool("read_file", path="config.txt"),
            tool("edit_file", path="config.txt", old_text="old", new_text="old"),
        ]
        model = WorkerWithFallback([
            tool("write_file", path="config.txt", content="old\n"),
            tool("write_file", path="check.sh", content=check),
        ], repeat, [
            tool("edit_file", path="config.txt", old_text="old", new_text="fixed"),
            tool("run_shell", command=command),
            tool("write_file", path="handoff.md", content="Fixed and checked.\n"),
            {"text": "The corrected configuration passed the check."},
        ])
        with TemporaryDirectory() as folder:
            root = Path(folder)
            service = StudioService(root, Settings(api_key="offline-fixture"))
            try:
                with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
                    run_id = service.start(msg("Repair the setting and write a handoff.", fresh(),
                        acceptance_files=["handoff.md"], acceptance_checks=[
                            {"kind": "contains", "path": "config.txt", "text": "fixed"}]))["id"]
                    detail = finished(service, run_id)
                events = load_events(root / run_id)
            finally:
                service.close()

        self.assertEqual(detail["run"]["status"], "completed", detail["run"].get("error"))
        commands = [e for e in events if e["kind"] == "tool_result" and e["tool"] == "run_shell"]
        self.assertGreaterEqual(len(commands), 3)
        self.assertEqual([e["exit_code"] for e in commands], [7] * (len(commands) - 1) + [0])
        self.assertTrue(all((e["pipeline_exit_code"], e["shell_exit_code"]) == (7, 0) for e in commands[:-1]))
        edits = [e for e in events if e["kind"] == "tool_result" and e["tool"] == "edit_file"]
        self.assertGreater(len(edits), 1)
        self.assertEqual([e["ok"] for e in edits], [False] * (len(edits) - 1) + [True])
        self.assertTrue(all(not e["artifacts"] for e in edits[:-1]))
        advisories = [e["message"] for e in events if e["kind"] == "progress_advisory"]
        self.assertTrue(any(all(s in note for s in ("restores previous state", "config.txt", "handoff.md"))
                            for note in advisories), advisories)
        self.assertTrue(any(item.get("content") in advisories
                            for messages, _ in model.requests for item in messages if item["role"] == "user"))
        self.assertEqual([e["report"]["verdict"] for e in events if e["kind"] == "acceptance"], ["passed"])

    def test_delivery_probe_includes_assignment_and_check_paths_without_running_acceptance(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "existing.txt").write_text("old")
            (root / "link").symlink_to(root / "existing.txt")
            checks = build_checks(["existing.txt"], [{"kind": "contains", "path": "result.txt", "text": "ready"}])
            probe = missing_file_probe(bind(checks, root, task_id="task", objective="goal"),
                                       root, ("notes.md", "link"))
            self.assertEqual(set(probe()), {"link", "notes.md", "result.txt"})
            (root / "notes.md").touch()
            self.assertEqual(set(probe()), {"link", "result.txt"})

            def custom_check(_workspace):
                raise AssertionError("a progress hint may not invoke acceptance")

            opaque = bind(custom_check, root, task_id="task", objective="goal")
            self.assertIsNone(missing_file_probe(opaque, root))
            self.assertEqual(missing_file_probe(opaque, root, ("next.txt",))(), ["next.txt"])
