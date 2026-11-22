"""Use the browser's HTTP contract to revise, discuss, and revise one project."""
import json
import threading
import time
import unittest
import uuid
from contextlib import closing
from http.client import HTTPConnection
from tempfile import TemporaryDirectory
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.studio_server import create_server
from miniagent.studio_service import StudioService


def tool(name, **arguments):
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


class StudioChatHTTPTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        service = StudioService(Path(folder.name) / "runs", Settings(api_key="unit-test-model"))
        self.addCleanup(service.close)
        self.service = service
        server = create_server(service, port=0)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        self.port = server.server_port
        self.token = self.get("/api/bootstrap")["csrf_token"]

    def exchange(self, method, path, payload=None):
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers.update({"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}",
                            "X-Studio-Token": self.token})
        with closing(HTTPConnection("127.0.0.1", self.port, timeout=5)) as conn:
            conn.request(method, path, body, headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())

    def get(self, path):
        status, payload = self.exchange("GET", path)
        self.assertEqual(status, 200, payload)
        return payload

    def send(self, message, conversation, *, files=()):
        payload = {"request_id": str(uuid.uuid4()), "mode": "single", "objective": message,
                   "acceptance_files": list(files), "conversation": conversation}
        status, data = self.exchange("POST", "/api/runs", payload)
        self.assertEqual(status, 202, data)
        run_id = data["run"]["id"]
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            detail = self.get(f"/api/runs/{run_id}")
            if detail["run"]["status"] in {"completed", "unverified", "failed", "cancelled"}:
                return run_id, detail
            time.sleep(0.02)
        self.fail(f"run {run_id} did not finish")

    def content(self, run_id, detail):
        artifact = next(a for a in detail["artifacts"] if a["name"] == "game.js")
        query = urlencode({"path": artifact["path"]})
        return self.get(f"/api/runs/{run_id}/artifact?{query}")["content"]

    def test_status_uses_group_receipts_in_edit_mode_without_starting_a_run(self):
        room = {"kind": "group", "members": ["worker", "reviewer"], "recipient": "worker", "intent": "edit"}
        with patch("miniagent.studio_service.DeepSeekProvider", side_effect=[
                ScriptedProvider([{"text": "draft"}]), ScriptedProvider([{"text": "other"}])]) as provider:
            original, _ = self.send("Ship the booking app", room, files=["index.html", "qa.md"])
            other, _ = self.send("Private other group task", room)
            workspace = self.service.runs_dir / original / "agents" / "worker"
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "qa.md").touch()  # An empty placeholder fails Studio's file acceptance.
            version = "b" * 64
            record = self.service._record_room_event
            record(original, "worker", "assignment", "implement", task_id="dev-1", recipient="worker")
            record(original, "worker", "member_report", "draft", task_id="dev-1", published=False,
                   handoff={"task_id": "dev-1", "role": "worker", "deliverable_paths": ["index.html"],
                            "reviewable_candidate": {"task_id": "dev-1", "candidate_version": version}})
            record(original, "worker", "assignment", "review", task_id="qa-1", recipient="reviewer")
            record(original, "reviewer", "member_report", "[BLOCKED]", task_id="qa-1",
                   disposition="blocked", handoff={"task_id": "qa-1", "role": "reviewer", "native_review": {
                       "state": "blocked", "target": "dev-1", "author": "worker", "reviewer": "reviewer",
                       "files": {"index.html": "a"*64}, "candidate_version": version,
                       "assessment": {"target": "dev-1", "candidate_version": version, "evaluator": "reviewer",
                                      "attempt_id": "attempt-qa", "verdict": "passed",
                                      "results": [{"criterion_id": "opens", "verdict": "passed"}]}}})
            payload = {"request_id": str(uuid.uuid4()), "mode": "team", "objective": "所以现在完成了吗？",
                       "conversation": {"parent_run_id": original, "recipient": "worker", "intent": "edit"}}
            from miniagent.studio_group_status import group_status
            from tests.test_room_reply import output
            room_view = self.service._conversation(self.service._directory(original), with_answers=False)
            snapshot = group_status(self.service, room_view)
            with patch("miniagent.studio_room_reply.DeepSeekProvider.chat", return_value=output(answer=snapshot["answer"])) as classify:
                status, read = self.exchange("POST", "/api/runs", payload)
                self.assertEqual(status, 202, read)
                self.assertEqual(read["run"]["id"], original)
                reply = read["message"]
                self.assertEqual(reply["disposition"], "reply")
                self.assertIn("reviewer 已签署通过", reply["text"])
                self.assertIn("仍待公开发布", reply["text"])
                self.assertIn("qa.md", reply["text"])
                self.assertNotIn("Private other", str(classify.call_args.args))
                self.assertEqual(self.exchange("POST", "/api/runs", payload)[1]["message"], reply)
                self.assertEqual(classify.call_count, 1)
                conflict, _ = self.exchange("POST", "/api/runs", {**payload, "objective": "Build something else"})
                self.assertEqual(conflict, 409)
            view = self.get("/api/runs/" + original)
            self.assertEqual(len(view["conversation"]["turns"]), 1)
            self.assertEqual(view["conversation"]["messages"], [reply])
            self.assertEqual(self.get("/api/runs/" + other)["conversation"]["messages"], [])
            self.assertEqual(provider.call_count, 2)

    def test_group_directs_one_responder_per_turn_and_revises_original_files_after_question(self):
        first_source = "const speed = 140; // milliseconds\n"
        initial = ScriptedProvider([tool("write_file", path="src/game.js", content=first_source),
                                    {"text": "Wrote the first game."}])
        ask = ScriptedProvider([tool("read_file", path="src/game.js"), {"text": "The speed is 140 ms."}])
        edit = ScriptedProvider([tool("edit_file", path="src/game.js", old_text="140", new_text="200"),
                                 {"text": "The new speed is 200 ms."}])
        with patch("miniagent.studio_service.DeepSeekProvider", side_effect=[initial, ask, edit]) as provider:
            first_id, first = self.send("Write the game", {"kind": "group", "members": ["worker", "reviewer"],
                                        "recipient": "worker", "intent": "edit"}, files=["src/game.js"])
            self.assertEqual(first["run"]["status"], "completed", first["run"])
            question_id, question = self.send("How fast does it move?", {"parent_run_id": first_id,
                                              "recipient": "reviewer", "intent": "question"})
            self.assertEqual(question["conversation"]["artifact_run_id"], first_id)
            reads = [e for e in question["events"] if e.get("kind") == "tool_result"
                     and e.get("tool") == "read_file"]
            self.assertEqual([e["ok"] for e in reads], [True])
            updated_id, updated = self.send("Slow it down to 200 ms", {"parent_run_id": question_id,
                                            "recipient": "worker", "intent": "edit"}, files=["src/game.js"])
            self.assertEqual(updated["run"]["status"], "completed", updated["run"])
            self.assertEqual(provider.call_count, 3)

        conversation = updated["conversation"]
        self.assertEqual(conversation["id"], first["conversation"]["id"])
        self.assertEqual(conversation["head_run_id"], updated_id)
        self.assertEqual(conversation["artifact_run_id"], updated_id)
        rooms = self.get("/api/conversations")["conversations"]
        self.assertEqual([(r["id"], r["head_run_id"], r["turn_count"]) for r in rooms],
                         [(conversation["id"], updated_id, 3)])
        self.assertEqual([t["recipient"] for t in conversation["turns"]], ["worker", "reviewer", "worker"])
        self.assertEqual([t["answer"] for t in conversation["turns"]],
                         ["Wrote the first game.", "The speed is 140 ms.", "The new speed is 200 ms."])
        self.assertEqual(self.content(first_id, first), first_source)
        self.assertEqual(self.content(updated_id, updated), first_source.replace("140", "200"))
        old = self.get(f"/api/runs/{first_id}")
        self.assertEqual(old["run"]["status"], "completed")
        self.assertEqual(old["acceptance"], first["acceptance"])
