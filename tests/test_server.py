"""HTTP API and preview origin, exercised over real sockets."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from miniagent.server import serve, templates
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings


def request(url, method="GET", body=None, header=True):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if header and method != "GET":
        req.add_header("X-Studio", "1")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.headers, exc.read()


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = Path(tempfile.mkdtemp())
        script = Script(lead=[
            reply("", call("write_file", path="index.html", content="<h1>Slides</h1><script>1</script>")),
            reply("", call("write_file", path="notes.md", content="# Notes")),
            reply("Built index.html and notes.md."),
        ])
        cls.studio = Studio(settings(tmp), provider_factory=lambda _s: FakeProvider(script))
        cls.main, cls.preview = serve(cls.studio, 0)
        cls.base = f"http://127.0.0.1:{cls.main.server_address[1]}"
        cls.preview_base = f"http://127.0.0.1:{cls.preview.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.studio.close()
        for server in (cls.main, cls.preview):
            server.shutdown()
            server.server_close()

    def test_ui_and_config(self):
        status, headers, body = request(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn(b"miniagent", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        status, _, body = request(self.base + "/api/config")
        self.assertEqual(json.loads(body)["preview_port"], self.preview.server_address[1])
        self.assertTrue(templates(), "Mid_Test templates ship with the project")

    def test_mutations_need_the_studio_header(self):
        status, _, _ = request(self.base + "/api/groups", "POST", {"name": "x"}, header=False)
        self.assertEqual(status, 403)

    def test_group_task_files_and_preview(self):
        status, _, body = request(self.base + "/api/groups", "POST", {
            "name": "Web", "lead": "lead", "members": [{"id": "lead", "name": "Lead", "prompt": "Builds."}]})
        self.assertEqual(status, 201, body)
        gid = json.loads(body)["id"]
        status, _, body = request(f"{self.base}/api/groups/{gid}/messages", "POST", {"text": "make slides"})
        self.assertEqual(json.loads(body)["route"], "task")
        for _ in range(200):
            state = json.loads(request(f"{self.base}/api/groups/{gid}")[2])
            if state["live"] is None and any(e["kind"] == "task_finished" for e in state["events"]):
                break
            time.sleep(0.05)
        kinds = [e["kind"] for e in state["events"]]
        self.assertIn("final", kinds)
        files = json.loads(request(f"{self.base}/api/groups/{gid}/files")[2])["files"]
        self.assertEqual({f["path"] for f in files}, {"index.html", "notes.md"})
        status, headers, body = request(f"{self.base}/api/groups/{gid}/files/index.html")
        self.assertTrue(headers["Content-Type"].startswith("text/plain"), "never executed on the API origin")
        status, headers, body = request(f"{self.preview_base}/{gid}/index.html")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(body, b"<h1>Slides</h1><script>1</script>")
        status, headers, body = request(f"{self.base}/api/groups/{gid}/archive.zip")
        self.assertEqual(headers["Content-Type"], "application/zip")
        status, _, _ = request(f"{self.preview_base}/{gid}/../../group.json")
        self.assertIn(status, {400, 404})

    def test_bad_group_request_is_a_400(self):
        status, _, body = request(self.base + "/api/groups", "POST", {"name": "x", "lead": "a", "members": []})
        self.assertEqual(status, 400)
        self.assertIn("members", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
