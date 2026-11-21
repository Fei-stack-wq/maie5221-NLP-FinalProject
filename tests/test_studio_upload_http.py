"""Exercise staged originals, message binding, and downloads over loopback HTTP."""
import http.client
import json
import socket
import threading
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import quote, urlencode

from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.studio_server import create_server
from miniagent.studio_service import StudioService
from miniagent.studio_uploads import UploadError, open_upload
from tests.support import cleanup, make_temp_dir
from tests.test_studio_service import finished


class StudioUploadHTTPTest(unittest.TestCase):
    def setUp(self):
        root = make_temp_dir("studio-upload-http")
        self.addCleanup(cleanup, root)
        self.service = StudioService(root, Settings(api_key="synthetic-test-key"))
        self.addCleanup(self.service.close)
        self.server = create_server(self.service, port=0)
        self.port = self.server.server_port
        self.headers = {"Origin": f"http://127.0.0.1:{self.port}", "X-Studio-Token": self.server.studio_token}
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def fetch(self, method, path, body=None, headers=None, *, half_close=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body, headers or {})
            if half_close:
                connection.sock.shutdown(socket.SHUT_WR)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def upload(self, name, content, *, half_close=False, **headers):
        route = "/api/uploads?" + urlencode({"name": name})
        headers = {**self.headers, "Content-Type": "application/octet-stream", **headers}
        status, _, data = self.fetch("POST", route, content, headers, half_close=half_close)
        return status, json.loads(data)

    def send(self, payload):
        status, _, data = self.fetch("POST", "/api/superagent/messages", json.dumps(payload),
                                     {**self.headers, "Content-Type": "application/json"})
        return status, json.loads(data)

    def test_same_name_binary_files_bound_to_single_message_and_replayed(self):
        name = '会议 "记录".bin'
        originals = [bytes(range(256)) * 1600, b""]
        refs = []
        for content in originals:
            status, body = self.upload(name, content)
            self.assertEqual(status, 201, body)
            refs.append(body["upload"])
            self.assertEqual((refs[-1]["name"], refs[-1]["size"]), (name, len(content)))
        self.assertNotEqual(refs[0]["id"], refs[1]["id"])

        payload = {"request_id": str(uuid.uuid4()), "message": "请确认收到文件。", "parent_run_id": None}
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=ScriptedProvider([{"text": "收到。"}])):
            status, _ = self.send({**payload, "attachments": [refs[0]["id"], refs[0]["id"]]})
            self.assertEqual(status, 400)
            status, _ = self.send({**payload, "attachments": [refs[0]["id"], "0" * 32]})
            self.assertEqual(status, 404)
            payload["attachments"] = [ref["id"] for ref in refs]
            status, body = self.send(payload)
            self.assertEqual(status, 202, body)
            run = body["run"]["id"]
            finished(self.service, run)
            replay_status, replay = self.send(payload)
            self.assertEqual((replay_status, replay["run"]["id"]), (202, run))

        room = self.service.create_channel({"request_id": str(uuid.uuid4()), "name": "其他会话",
                                            "lead": "worker", "members": ["worker", "reviewer"]})
        for ref, content in zip(refs, originals):
            path = f"/api/runs/{run}/attachments/{ref['id']}"
            status, headers, data = self.fetch("GET", path)
            self.assertEqual((status, data), (200, content))
            self.assertEqual(headers["Content-Type"], "application/octet-stream")
            self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
            self.assertIn(quote(name, safe=""), headers["Content-Disposition"])
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            status, head, data = self.fetch("HEAD", path)
            self.assertEqual((status, data), (200, b""))
            for field in ("Content-Type", "Content-Length", "Content-Disposition"):
                self.assertEqual(head[field], headers[field])
            self.assertEqual(int(head["Content-Length"]), len(content))
            self.assertEqual(self.fetch("GET", f"/api/runs/{room['id']}/attachments/{ref['id']}")[0], 404)

    def test_interruption_and_unsafe_requests_leave_original_intact(self):
        status, original = self.upload("notes.md", b"first draft")
        self.assertEqual(status, 201)
        original = original["upload"]
        status, error = self.upload("notes.md", b"cut", half_close=True, **{"Content-Length": "200"})
        self.assertEqual((status, error["code"]), (400, "incomplete_upload"))
        for name in ("../notes.md", r"..\notes.md", "bad\r\nheader.html", "file\x00.txt", ""):
            with self.subTest(name=name):
                self.assertEqual(self.upload(name, b"bad")[0], 400)
        self.assertEqual(self.upload("notes.md", b"x", Origin="https://unrelated.example")[0], 403)
        self.assertEqual(self.upload("notes.md", b"x", **{"X-Studio-Token": "wrong"})[0], 403)
        self.assertEqual(self.upload("notes.md", b"x", **{"Content-Type": "multipart/form-data"})[0], 415)
        store = self.service.runs_dir / ".studio-uploads"
        self.assertEqual([p.name for p in store.iterdir()], [original["id"]])
        ref, stream = open_upload(self.service.runs_dir, original["id"])
        with stream:
            self.assertEqual((ref, stream.read()), (original, b"first draft"))

        blob = store / original["id"] / "data"
        outside = self.service.runs_dir / "outside.txt"
        outside.write_bytes(b"private txt")
        blob.unlink()
        blob.symlink_to(outside)
        with self.assertRaises(UploadError) as caught:
            open_upload(self.service.runs_dir, original["id"])
        self.assertEqual(caught.exception.status, 409)
