import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode
import uuid

from miniagent.agent import Agent
from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.rao import Observation
from miniagent.studio_service import StudioError, StudioService
from miniagent.tool_activity import MAX_ACTIVITY, project_activity, target_for
from miniagent.tools import ToolRegistry
from tests import test_studio_chat_http
from tests.test_studio_service import finished


class ActivityTests(unittest.TestCase):
    def test_live_call_in_batch_finishes_with_same_identity_and_no_private_payload(self):
        with TemporaryDirectory() as directory:
            service = StudioService(Path(directory) / "runs", Settings(api_key="test-secret-key"))
            self.addCleanup(service.close)
            entered, release = threading.Event(), threading.Event()
            dispatch = ToolRegistry.dispatch

            def held(registry, name, arguments, context):
                if name == "read_file":
                    entered.set()
                    release.wait(5)
                return dispatch(registry, name, arguments, context)

            private = "a-private-marker-only-the-file-reader-should-see"
            provider = ScriptedProvider([
                {"tool_calls": [
                    {"name": "write_file", "arguments": {"path": "note.md", "content": private}},
                    {"name": "read_file", "arguments": {"path": "note.md"}},
                ]},
                {"text": "Done."},
            ])
            with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider), patch.object(ToolRegistry, "dispatch", held):
                try:
                    run = service.start({"request_id": str(uuid.uuid4()), "mode": "single", "objective": "Create a note.",
                                         "acceptance_files": ["note.md"], "conversation": {"kind": "dm", "members": ["worker"],
                                                                                         "recipient": "worker", "intent": "edit"}})
                    self.assertTrue(entered.wait(3))
                    pending = service.tool_activity(run["id"])
                    self.assertEqual([(row["tool"], row["status"]) for row in pending["calls"]],
                                     [("write_file", "succeeded"), ("read_file", "running")])
                finally:
                    release.set()
                detail = finished(service, run["id"])
            calls = detail["activity"]["calls"]
            self.assertNotIn(private, json.dumps(detail["activity"]))
            self.assertEqual([r["id"] for r in calls], [r["id"] for r in pending["calls"]])
            self.assertEqual([r["summary"] for r in calls], ["note.md", "note.md"])
            self.assertEqual(calls[1]["output"], f"已读取 {len(private)} 字符")
            self.assertEqual(calls[1]["role"], "worker")
            self.assertEqual(calls[1]["status"], "succeeded")
            for artifact in detail["artifacts"]:
                if artifact["name"] == "note.md":
                    self.assertEqual(service.artifact_excerpt(run["id"], artifact["path"])["content"], private)

    def test_only_typed_public_fields_and_real_terminal_receipts_are_projected(self):
        start = dict(kind="tool_start", task_id="task", attempt_id="a", tool="http_get", step=2,
                     action_id="id", agent="writer", target="www.example.org", ts=10,
                     query="private", raw_arguments={"token": "private"})
        done = {**start, "kind": "tool_result", "ts": 12, "ok": False,
                "error_class": "A_TOOL_TIMEOUT", "preview": "private", "stdout": "private"}
        interrupted = project_activity([start], active=False)["calls"][0]
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(project_activity([start, {"kind": "task_end", "task_id": "task", "attempt_id": "a"}],
                                          active=True)["calls"][0]["status"], "interrupted")
        result = project_activity([start, done])["calls"][0]
        self.assertEqual((result["status"], result["error"], result["duration_ms"]), ("failed", "工具执行超时", 2000))
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(target_for("http_get", {"url": "https://user:secret@example.org/path?token=secret"}), "example.org")
        for path in ("../keys", ".env", "sk-" + "A" * 40, "/private/name", "q?token=private"):
            self.assertEqual(target_for("read_file", {"path": path}), "")
        self.assertEqual(target_for("run_shell", {"command": "echo private"}), "")

    def test_direct_root_actor_can_show_single_complete_scalars_only(self):
        thread = {"kind": "dm", "members": ["worker"], "recipient": "worker"}
        event = {"kind": "tool_result", "ok": True, "tool": "run_python", "operation": "run_python",
                 "agent": "worker", "task_id": "turn", "root_id": "turn", "level": "goal", "parent_task_id": None,
                 "complete_preview": True, "exit_code": 0, "stdout_bytes": 3, "stderr_bytes": 0,
                 "preview": "exit_code=0\nstdout:\n42\n"}

        def summary(changes, room=thread):
            return project_activity([{**event, **changes}], run_id="turn", thread=room)["calls"][0]["summary"]

        self.assertEqual(summary({}), "42")
        for value in ("True", "false", "-3.14", "0"):
            self.assertEqual(summary({"stdout_bytes": len(value) + 1, "preview": "exit_code=0\nstdout:\n" + value + "\n"}), value)
        for changes in ({"agent": "delegate"}, {"level": "subtask"}, {"root_id": "different"},
                        {"parent_task_id": "parent"}, {"task_id": "other"}, {"complete_preview": False},
                        {"complete_preview": None}, {"stdout_bytes": 5}, {"stderr_bytes": 2},
                        {"truncated": True}, {"exit_code": 1}, {"tool": "read_file"}, {"ok": False}):
            self.assertEqual(summary(changes), "", changes)
        for value in ("123456", "private text", "sk-" + "a" * 45, "42\nTrue", "42\nstderr:\n42"):
            self.assertEqual(summary({"preview": "exit_code=0\nstdout:\n" + value, "stdout_bytes": len(value)}), "")
        self.assertEqual(summary({}, {**thread, "kind": "group", "members": ["worker", "reviewer"]}), "")
        self.assertEqual(summary({}, {}), "")

    def test_management_name_comes_from_validated_host_receipt_in_superagent(self):
        identity = dict(task_id="turn", root_id="turn", level="goal", parent_task_id=None)
        actor = Mock()
        actor.name = "worker"
        thread = dict(kind="dm", members=["worker"], recipient="worker", superagent=True)
        for tool, kind, identifier in (("create_studio_role", "role", "scheduler"),
                                       ("create_studio_group", "group", "group-" + "a" * 32)):
            entity = dict(kind=kind, id=identifier, name="预约团队", prompt="private instructions")
            obs = Observation(step_id="s", source=tool, ok=True, content="untrusted preview",
                              meta={"entity": entity})
            Agent._trace_tool(actor, identity, 1, obs, tool)
            receipt = {**actor.tracer.event.call_args.kwargs, "kind": "tool_result"}

            def summary(changes=None, room=thread):
                return project_activity([{**receipt, **(changes or {})}], run_id="turn", thread=room)["calls"][0]["summary"]

            self.assertEqual(summary(), "预约团队")
            self.assertNotIn("prompt", receipt["created_entity"])
            for changes in ({"ok": False}, {"agent": "reviewer"}, {"created_entity": None},
                            {"created_entity": {**entity, "id": "../outside"}},
                            {"created_entity": {**entity, "name": "token=secret"}},
                            {"created_entity": {**entity, "name": "sk-" + "a" * 40}}):
                self.assertEqual(summary(changes), "", changes)
            self.assertEqual(summary(room={**thread, "superagent": False}), "")

    def test_process_stdout_is_visible_only_to_its_direct_turn(self):
        with TemporaryDirectory() as directory:
            service = StudioService(Path(directory) / "runs", Settings(api_key="test-key"))
            self.addCleanup(service.close)
            for kind, members in (("dm", ["worker"]), ("group", ["worker", "reviewer"])):
                provider = ScriptedProvider([{"tool_calls": [
                    {"name": "run_python", "arguments": {"code": "print(6 * 7)"}},
                    {"name": "run_python", "arguments": {"code": "print(True)"}},
                    {"name": "run_python", "arguments": {"code": "print('private string')"}},
                ]}, {"text": "Done."}])
                with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
                    run = service.start({"request_id": str(uuid.uuid4()), "mode": "single", "objective": "Calculate.",
                                         "conversation": {"kind": kind, "members": members, "recipient": "worker", "intent": "edit"}})
                    detail = finished(service, run["id"])
                activity = service.tool_activity(run["id"])
                self.assertEqual(activity, detail["activity"])
                self.assertNotIn("private string", json.dumps(activity))
                self.assertEqual([c["summary"] for c in activity["calls"]], ["42", "True", ""] if kind == "dm" else ["", "", ""])

    def test_bounded_history_and_legacy_calls_with_reused_ids(self):
        events = [{"kind": "tool_result", "tool": "write_file", "tool_call_id": "reused",
                   "step": i, "ok": True, "artifacts": ["real.md"], "preview": "private"} for i in range(MAX_ACTIVITY + 1)]
        history = project_activity(events)
        self.assertTrue(history["truncated"])
        self.assertEqual(len({row["id"] for row in history["calls"]}), MAX_ACTIVITY)
        self.assertTrue(all(row["summary"] == "real.md" for row in history["calls"]))
        self.assertEqual(history["calls"], project_activity(events[1:])["calls"])

    def test_excerpt_uses_artifact_access_and_hides_credentials(self):
        with TemporaryDirectory() as directory:
            service = StudioService(Path(directory) / "runs", Settings(api_key="unit-secret"))
            self.addCleanup(service.close)
            project = service.runs_dir / "existing" / "agents" / "worker"
            project.mkdir(parents=True)
            text = "first line\n" + "a\n" * 5000
            (project / "long.md").write_text(text)
            (project / "sensitive.md").write_text("Authorization: Bearer something")
            (project / "configured.md").write_text("unit-secret")
            (project / "linked.md").symlink_to(project / "long.md")
            read = lambda path: service.artifact_excerpt("existing", "agents/worker/" + path)
            excerpt = read("long.md")
            self.assertTrue(excerpt["truncated"])
            self.assertEqual(len(excerpt["content"].splitlines()), 12)
            for name in ("sensitive.md", "configured.md", ".env", "../long.md", "linked.md"):
                with self.assertRaises(StudioError, msg=name):
                    read(name)


class ActivityHTTPTests(unittest.TestCase):
    setUp = test_studio_chat_http.StudioChatHTTPTest.setUp
    exchange = test_studio_chat_http.StudioChatHTTPTest.exchange
    get = test_studio_chat_http.StudioChatHTTPTest.get
    send = test_studio_chat_http.StudioChatHTTPTest.send

    def test_group_chat_has_activity_endpoint_and_literal_text_excerpt(self):
        provider = ScriptedProvider([{"tool_calls": [{"name": "write_file", "arguments":
                                     {"path": "note.md", "content": "<b>customer text</b>"}}]}, {"text": "Done."}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            rid, detail = self.send("Create a note.", {"kind": "group", "members": ["worker", "reviewer"],
                                  "recipient": "worker", "intent": "edit"}, files=["note.md"])
        self.assertEqual(self.get(f"/api/runs/{rid}/activity"), detail["activity"])
        self.assertEqual(detail["activity"]["calls"][0]["tool"], "write_file")
        path = next(a["path"] for a in detail["conversation"]["turns"][0]["artifacts"] if a["name"] == "note.md")
        response = self.get(f"/api/runs/{rid}/excerpt?" + urlencode({"path": path}))
        self.assertEqual(response["content"], "<b>customer text</b>")
        self.assertEqual(self.exchange("GET", f"/api/runs/{rid}/excerpt?path=studio.json")[0], 400)
