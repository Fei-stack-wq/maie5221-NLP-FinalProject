"""Exercise two launcher processes against one data directory and a dynamic port."""
import contextlib
import io
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
from miniagent.instance import running_studio_url, wait_for_studio


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
