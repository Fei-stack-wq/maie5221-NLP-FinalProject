"""Exercise two launcher processes against one data directory and a dynamic port."""
import contextlib
import io
import http.client
import shutil
import threading
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from miniagent.cli import main
from miniagent.instance import PACKAGE, registered_studio, running_studio_url, session_lock, wait_for_instance, wait_for_studio
from miniagent.studio import Studio, StudioError
from tests.helpers import FakeProvider, reply, settings
from tests.test_server import request
from tests.test_team import wait_for


class StudioLauncherTest(unittest.TestCase):
    def test_repeated_launch_reuses_only_the_owner_of_this_directory(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as occupied:
            runs = Path(tmp)
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            requested = occupied.getsockname()[1]
            args = ["--runs-dir", tmp, "studio", "--reuse", "--port", str(requested)]
            process = subprocess.Popen([sys.executable, "-m", "miniagent", *args, "--no-browser"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            try:
                url = wait_for_studio(runs)
                self.assertIsNotNone(url)
                self.assertNotEqual(url, f"http://127.0.0.1:{requested}/")
                with patch("miniagent.cli.webbrowser.open") as browser:
                    self.assertEqual(main(args), 0)
                    browser.assert_called_once_with(url)
                    self.assertEqual(main([*args, "--no-browser"]), 0)
                    browser.assert_called_once()
                    with contextlib.redirect_stderr(io.StringIO()) as error:
                        self.assertEqual(main([*args, "--strict-port"]), 1)
                    self.assertIn(url, error.getvalue())
                    browser.assert_called_once()
                self.assertIsNone(process.poll())
                record = runs / ".studio-instance.json"
                wrong_room = {"port": json.loads(record.read_text())["port"], "id": "0"*32}
                record.write_text(json.dumps(wrong_room))
                self.assertIsNone(running_studio_url(runs))
            finally:
                if process.poll() is None:
                    if sys.platform == "win32":
                        process.terminate()
                    else:
                        process.send_signal(signal.SIGINT)
                _, error = process.communicate(timeout=12)
            if sys.platform != "win32":
                self.assertEqual(process.returncode, 0, error)
                self.assertFalse(record.exists())

    def test_strict_port_refuses_an_unrelated_listener(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            args = ["--runs-dir", tmp, "studio", "--reuse", "--strict-port", "--port", str(port)]
            with patch("miniagent.cli.webbrowser.open") as browser, contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(main(args), 1)
                browser.assert_not_called()
            self.assertIn(f"端口 {port} 已被占用", error.getvalue())
            self.assertFalse((Path(tmp) / ".studio-instance.json").exists())

    def test_outdated_and_invalid_registrations(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            record = runs / ".studio-instance.json"
            for data in ("", "[]", '{"port": 65536}', '{"port":"https://example.org","id":"bogus"}'):
                record.write_text(data)
                self.assertIsNone(running_studio_url(runs))

    def test_runtime_update_during_http_write_then_idle_update_on_same_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "miniagent"
            shutil.copytree(PACKAGE, package, ignore=shutil.ignore_patterns("__pycache__"))
            runs = root / "runs"
            args = [sys.executable, "-m", "miniagent", "--runs-dir", str(runs),
                    "studio", "--port", "0", "--reuse", "--no-browser"]
            process = subprocess.Popen(args, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            self.addCleanup(process.stderr.close)
            try:
                first = wait_for_instance(runs)
                self.assertIsNotNone(first)
                url = first.url.rstrip("/")
                asset = package / "web" / "launcher-test.txt"
                asset.write_text("busy update")
                token = json.loads(request(url + "/api/bootstrap")[2])["token"]
                body = json.dumps({"name":"Saved through update","members":[{"id":"lead","name":"Lead","prompt":"Lead"}],"lead":"lead"}).encode()
                # Hold a real authenticated POST after admission and before its JSON has arrived.
                with contextlib.closing(http.client.HTTPConnection("127.0.0.1", first.port, timeout=8)) as client:
                    client.putrequest("POST", "/api/groups")
                    for key, value in {"Origin":url,"X-Studio-Token":token,"Content-Length":str(len(body))}.items():
                        client.putheader(key, value)
                    client.endheaders()
                    wait_for(lambda: registered_studio(runs).busy)
                    result = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=8, check=True)
                    self.assertIn("自动切换", result.stdout)
                    wait_for(lambda: request(url + "/api/instance")[1].get("X-Studio-Update") == "pending")
                    self.assertEqual(registered_studio(runs).id, first.id)
                    client.send(body)
                    response = client.getresponse()
                    self.assertEqual(response.status, 201)
                    gid = json.loads(response.read())["id"]
                second = wait_for_instance(runs, previous=first.id, timeout=8)
                self.assertEqual(second.url, first.url)
                self.assertEqual(request(url + "/static/launcher-test.txt")[2], b"busy update")
                self.assertIn(gid, [r["id"] for r in json.loads(request(url + "/api/groups")[2])])

                asset.write_text("idle update")
                result = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=12, check=True)
                third = registered_studio(runs)
                self.assertEqual(third.url, first.url)
                self.assertNotIn(third.id, (first.id,second.id))
                self.assertEqual(request(url+"/static/launcher-test.txt")[2], b"idle update")
                (root / "notes.md").write_text("Only documentation changed")
                subprocess.run(args, cwd=root, capture_output=True, timeout=8, check=True)
                self.assertEqual(registered_studio(runs).id, third.id)
            finally:
                process.terminate()
                process.communicate(timeout=12)

    def test_update_waits_for_running_and_queued_work(self):
        release = threading.Event()
        def respond(agent, messages, tools, options):
            if not release.wait(8):
                raise AssertionError("test did not release the worker")
            return reply("Delivered.")
        with tempfile.TemporaryDirectory() as tmp:
            studio = Studio(settings(Path(tmp)), lambda _: FakeProvider(respond))
            try:
                room = studio.create_group("Team",[{"id":"lead","name":"Lead","prompt":"Lead"}],"lead")["id"]
                first = studio.post(room, "First task", queue=True)["task_id"]
                second = studio.post(room, "Next task", queue=True)["task_id"]
                drain = threading.Thread(target=studio.drain_for_update, daemon=True)
                drain.start()
                wait_for(lambda: studio.update_pending)
                with studio.writing_request():
                    self.assertTrue(drain.is_alive())
                    release.set()
                    wait_for(lambda: not studio._running)
                    self.assertTrue(drain.is_alive())
                drain.join(timeout=5)
                self.assertFalse(drain.is_alive())
                self.assertEqual([(r["id"],r["status"]) for r in studio.tasks(room)],
                                 [(first,"completed"),(second,"completed")])
                with self.assertRaises(StudioError) as error, studio.writing_request():
                    self.fail("accepted a write after drain")
                self.assertEqual(error.exception.status, 503)
            finally:
                release.set()
                studio.close()

    def test_last_owner_exits_during_lookup(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as old:
            old.enter_context(session_lock(Path(tmp)))
            def finish_shutdown(directory):
                old.close()
                return None
            with patch("miniagent.instance.wait_for_instance",side_effect=finish_shutdown), \
                 patch("miniagent.cli.cmd_studio",return_value=0) as start:
                self.assertEqual(main(["--runs-dir",tmp,"studio","--reuse","--no-browser"]),0)
                start.assert_called_once()
