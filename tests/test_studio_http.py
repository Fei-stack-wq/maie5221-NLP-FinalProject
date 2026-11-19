"""Loopback HTTP integration: requests must not widen local execution access."""
from __future__ import annotations

import http.client
import io
import json
import threading
import unittest
import uuid
import zipfile
from urllib.parse import quote
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioService
from miniagent.studio_server import create_server
from tests.support import cleanup, make_temp_dir
from tests.test_studio_service import finished


class StudioHTTPTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("studio-http")
        self.service = StudioService(self.root / "runs", Settings(api_key="synthetic-server-only-key"))
        self.server = create_server(self.service, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.service.close()
        cleanup(self.root)

    def fetch(self, method="GET", path="/api/bootstrap", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), raw
        finally:
            connection.close()

    def post(self, value, **header_overrides):
        headers = {"Content-Type": "application/json", "Origin": self.origin,
                   "X-Studio-Token": self.server.studio_token}
        headers.update(header_overrides)
        headers = {key: value for key, value in headers.items() if value is not None}
        return self.fetch("POST", "/api/runs", json.dumps(value).encode(), headers)

    def test_superagent_http_replay_head_and_same_origin_guard(self):
        from miniagent.providers import ScriptedProvider
        endpoint = "/api/superagent/messages"
        payload = json.dumps({"request_id": str(uuid.uuid4()), "message": "你好", "parent_run_id": None})
        headers = {"Content-Type": "application/json", "Origin": self.origin, "X-Studio-Token": self.server.studio_token}
        self.assertEqual(json.loads(self.fetch(path="/api/superagent")[2])["head_run_id"], None)
        self.assertEqual(self.fetch("POST", endpoint, payload, {"Content-Type": "application/json"})[0], 403)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=ScriptedProvider([{"text": "你好"}])):
            status, _, body = self.fetch("POST", endpoint, payload, headers)
            self.assertEqual(status, 202, body)
            run_id = json.loads(body)["run"]["id"]
            finished(self.service, run_id)
        self.assertEqual(json.loads(self.fetch(path="/api/superagent")[2]),
                         {"conversation_id": run_id, "head_run_id": run_id})
        self.assertEqual(json.loads(self.fetch("POST", endpoint, payload, headers)[2])["run"]["id"], run_id)

    def test_channel_create_and_unpin_over_http_reuses_real_empty_conversation(self):
        listing = "/api/directory/chats"
        endpoint = "/api/directory/channels"
        headers = {"Content-Type": "application/json", "Origin": self.origin, "X-Studio-Token": self.server.studio_token}
        request = json.dumps({"request_id": str(uuid.uuid4()), "name": "维护组", "lead": "worker",
                              "members": ["worker", "reviewer"]})
        self.assertEqual(json.loads(self.fetch(path=listing)[2]), {"recent": [], "channels": []})
        self.assertEqual(self.fetch("POST", endpoint, request, {"Content-Type": "application/json"})[0], 403)
        status, _, body = self.fetch("POST", endpoint, request, headers)
        self.assertEqual(status, 200, body)
        row = json.loads(body)["channel"]
        self.assertEqual(row["id"], row["head_run_id"])
        self.assertEqual(json.loads(self.fetch(path=listing)[2])["channels"], [row])
        details = json.loads(self.fetch(path="/api/runs/" + row["id"])[2])["conversation"]
        self.assertTrue(details["can_continue"])
        self.assertEqual(details["turns"], [])
        roster = endpoint + "/" + row["id"] + "/members"
        self.assertEqual(json.loads(self.fetch(path=roster)[2])["revision"], 0)
        change = json.dumps({"revision": 0, "members": ["reviewer"], "lead": "reviewer"})
        self.assertEqual(self.fetch("POST", roster, change, {"Content-Type": "application/json"})[0], 403)
        status, _, body = self.fetch("POST", roster, change, headers)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["members"], ["reviewer"])
        self.assertEqual(json.loads(self.fetch("POST", roster, change, headers)[2])["code"], "stale_membership")
        status, _, body = self.fetch("POST", endpoint + "/" + row["id"], '{"pinned":false}', headers)
        self.assertEqual(status, 200, body)
        self.assertFalse(json.loads(body)["channel"]["pinned"])
        self.assertEqual(len(json.loads(self.fetch(path=listing)[2])["recent"]), 1)
        self.assertEqual(json.loads(self.fetch(path=listing)[2])["channels"], [])

    def test_native_folder_picker_uses_existing_origin_and_token_gate(self):
        route = "/api/workspaces/pick"
        headers = {"Origin": self.origin, "Content-Type": "application/json",
                   "X-Studio-Token": self.server.studio_token}
        with patch("miniagent.studio_workspace.pick_directory", return_value="/some/user folder") as pick:
            blocked = self.fetch("POST", route, b"{}", {**headers, "Origin": "http://example.test"})
            self.assertEqual(blocked[0], 403)
            pick.assert_not_called()
            self.assertEqual(self.fetch("POST", route, b'{"path":"../"}', headers)[0], 400)
            status, _, body = self.fetch("POST", route, b"{}", headers)
            self.assertEqual((status, json.loads(body)), (200, {"path": "/some/user folder"}))

    def test_exact_file_download_refuses_links_and_quotes_unicode_name(self):
        run = self.service.start({"request_id": str(uuid.uuid4()), "mode": "demo"})
        finished(self.service, run["id"])
        file = self.service.runs_dir / run["id"] / "agents/worker"
        name = "项目 one.md"
        (file / name).write_text("frozen snapshot")
        route = "/api/runs/" + run["id"] + "/download?path="
        status, headers, content = self.fetch(path=route + quote("agents/worker/" + name))
        self.assertEqual((status, content), (200, b"frozen snapshot"))
        self.assertIn(quote(name), headers["Content-Disposition"])
        (file / "linked.md").symlink_to(file / name)
        self.assertEqual(self.fetch(path=route + "agents/worker/linked.md")[0], 400)
        self.assertEqual(self.fetch(path=route + "studio.json")[0], 400)

    def test_bootstrap_is_local_and_never_contains_api_key(self):
        status, headers, body = self.fetch()
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertEqual(value["csrf_token"], self.server.studio_token)
        self.assertTrue(value["provider_ready"])
        self.assertNotIn(b"synthetic-server-only-key", body)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertIn("no-store", headers.get("Cache-Control", ""))

    def test_conversation_index_groups_turns_without_reading_run_traces(self):
        rows = [
            ("older", "single", "original project", None),
            ("group", "single", "Review a draft", {"id": "group", "ordinal": 1}),
            ("revision", "single", "Make the changes", {"id": "group", "ordinal": 2}),
            ("dag", "team", "Team plan", None),
        ]
        for index, (run_id, mode, title, thread) in enumerate(rows):
            directory = self.service.runs_dir / run_id
            directory.mkdir()
            meta = {"title": title, "mode": mode, "request": {"objective": title},
                    "state": "unverified", "created_at": f"2026-11-17T00:0{index}:00Z"}
            if thread:
                meta["thread"] = {"kind": "group", "members": ["worker", "reviewer"],
                                  "intent": "edit", "recipient": "worker", "base_run_id": None,
                                  "parent_run_id": "group" if index == 2 else None, **thread}
            (directory / "studio.json").write_text(json.dumps(meta), encoding="utf-8")
        with patch.object(self.service, "_events", side_effect=AssertionError("index scanned traces")):
            status, _, body = self.fetch(path="/api/conversations")
        self.assertEqual(status, 200, body)
        groups = json.loads(body)["conversations"]
        self.assertEqual([item["id"] for item in groups], ["group", "older"])
        self.assertEqual(groups[0]["title"], "Review a draft")
        self.assertEqual(groups[0]["head_run_id"], "revision")
        self.assertEqual(groups[0]["turn_count"], 2)
        self.assertEqual(groups[1]["kind"], "dm")

    def test_assets_are_exact_allowlist_with_local_content_policy(self):
        for path, mime in [("/", "text/html"), ("/app.js", "javascript"), ("/style.css", "text/css")]:
            with self.subTest(path=path):
                status, headers, body = self.fetch(path=path)
                self.assertEqual(status, 200)
                self.assertIn(mime, headers.get("Content-Type", ""))
                self.assertTrue(body)
                self.assertIn("script-src 'self'", headers.get("Content-Security-Policy", ""))
                self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        for path in ["/.env", "/../config.py", "/%2e%2e/config.py", "/miniagent/config.py", "/index.html/../.env"]:
            with self.subTest(path=path):
                self.assertIn(self.fetch(path=path)[0], {400, 403, 404})

    def test_start_returns_id_and_replay_returns_same_run(self):
        payload = {"request_id": str(uuid.uuid4()), "mode": "demo"}
        status, _, body = self.post(payload)
        self.assertEqual(status, 202, body)
        run_id = json.loads(body)["run"]["id"]
        status, _, body = self.post(payload)
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["run"]["id"], run_id)
        status, _, body = self.fetch(path="/api/runs")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["runs"]), 1)

    def test_post_requires_both_same_origin_and_session_token(self):
        for override in [{"X-Studio-Token": None}, {"X-Studio-Token": "wrong"},
                         {"Origin": "https://attacker.example"}, {"Origin": "null"},
                         {"Origin": None}, {"Origin": f"http://localhost:{self.port + 1}"}]:
            with self.subTest(override=override):
                status, _, _ = self.post({"request_id": str(uuid.uuid4()), "mode": "demo"}, **override)
                self.assertEqual(status, 403)
        self.assertEqual(self.service.list_runs()["runs"], [])

    def test_host_guard_rejects_dns_rebinding_even_with_valid_post_token(self):
        status, _, _ = self.fetch(headers={"Host": f"attacker.example:{self.port}"})
        self.assertEqual(status, 403)
        status, _, _ = self.post({"request_id": str(uuid.uuid4()), "mode": "demo"}, Host=f"attacker.example:{self.port}")
        self.assertEqual(status, 403)
        self.assertEqual(self.service.list_runs()["runs"], [])

    def test_cross_site_simple_content_type_cannot_start_execution(self):
        status, _, _ = self.post({"request_id": str(uuid.uuid4()), "mode": "demo"}, **{"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertEqual(self.service.list_runs()["runs"], [])

    def test_malformed_and_oversize_json_are_rejected_without_execution(self):
        headers = {"Content-Type": "application/json", "Origin": self.origin,
                   "X-Studio-Token": self.server.studio_token}
        status, _, _ = self.fetch("POST", "/api/runs", b"{", headers)
        self.assertEqual(status, 400)
        # Reject the declared size before reading a potentially unbounded body.
        status, _, _ = self.fetch("POST", "/api/runs", b"", {**headers, "Content-Length": str(1024 * 1024)})
        self.assertEqual(status, 413)
        self.assertEqual(self.service.list_runs()["runs"], [])

    def test_error_response_does_not_reflect_server_exception_or_stack(self):
        with patch.object(self.service, "bootstrap", side_effect=RuntimeError("synthetic-private-path-and-secret")):
            status, _, body = self.fetch()
        self.assertEqual(status, 500)
        self.assertNotIn(b"synthetic-private-path-and-secret", body)
        self.assertNotIn(b"Traceback", body)
        self.assertIsInstance(json.loads(body)["error"], str)

    def test_unknown_and_traversal_requests_do_not_read_arbitrary_paths(self):
        for path in ["/api/runs/missing", "/api/runs/../artifact?path=.env",
                     "/api/runs/%2e%2e/artifact?path=.env",
                     "/api/runs/missing/artifact?path=" + quote("../../.env")]:
            with self.subTest(path=path):
                status, _, body = self.fetch(path=path)
                self.assertIn(status, {400, 403, 404})
                self.assertNotIn(b"synthetic-server-only-key", body)

    def test_server_refuses_non_loopback_binding(self):
        with self.assertRaises((ValueError, RuntimeError)):
            unexpected = create_server(self.service, host="0.0.0.0", port=0)
            unexpected.server_close()

    def test_cancel_requires_csrf_and_is_idempotent_for_finished_runs(self):
        run = self.service.start({"request_id": str(uuid.uuid4()), "mode": "demo"})
        original = finished(self.service, run["id"])
        path = f"/api/runs/{run['id']}/cancel"
        headers = {"Content-Type": "application/json", "Origin": self.origin}
        self.assertEqual(self.fetch("POST", path, b"{}", headers)[0], 403)
        headers["X-Studio-Token"] = self.server.studio_token
        status, _, body = self.fetch("POST", path, b"{}", headers)
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["run"]["status"], "completed")
        self.assertEqual(self.service.detail(run["id"])["events"], original["events"])
        self.assertEqual(self.fetch("POST", path, b'{"force":true}', headers)[0], 400)
        self.assertEqual(self.fetch("POST", path, b"null", headers)[0], 400)

    def test_bundle_response_is_attachment_and_preserves_full_workspace_file(self):
        run = self.service.start({"request_id": str(uuid.uuid4()), "mode": "demo"})
        finished(self.service, run["id"])
        status, headers, raw = self.fetch(path=f"/api/runs/{run['id']}/artifacts.zip")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertIn("attachment;", headers["Content-Disposition"])
        self.assertIn(run["id"], headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(archive.namelist(), ["notes.md"])
            self.assertIn("演示产物", archive.read("notes.md").decode())
        self.assertEqual(self.fetch(path="/api/runs/missing/artifacts.zip")[0], 404)


if __name__ == "__main__":
    unittest.main()
