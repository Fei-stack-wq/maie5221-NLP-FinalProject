import os
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from miniagent.agent import Agent
from miniagent.config import Limits, Settings
from miniagent.member_processes import managed_member_processes
from miniagent.metrics import Tracer
from miniagent.providers import ScriptedProvider
from miniagent.sandbox import Sandbox
from miniagent.tools import ToolContext, build_default_registry


@unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
class ShellToolTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.ctx = ToolContext(sandbox=Sandbox(self.root), limits=Limits(tool_timeout_seconds=4))
        self.registry = build_default_registry()

    def run_command(self, command, **options):
        return self.registry.dispatch("run_shell", {"command": command, **options}, self.ctx)

    def test_multifile_command_pipeline_and_failing_test(self):
        app = self.root / "app dir"
        app.mkdir()
        (app / "core.py").write_text("def area(w, h): return w*h\n")
        (app / "test_core.py").write_text(
            "import unittest\nfrom core import area\n"
            "class TestArea(unittest.TestCase):\n def test_area(self): self.assertEqual(area(3,4),12)\n")
        passed = self.run_command("python3 -m unittest discover && printf 'room b\nroom a\n' | sort > sorted.txt", cwd="app dir")
        self.assertTrue(passed.ok, passed.content)
        self.assertIn("Ran 1 test", passed.content)
        self.assertIn("OK", passed.content)
        self.assertEqual(passed.meta["cwd"], "app dir")
        self.assertEqual((app / "sorted.txt").read_text(), "room a\nroom b\n")
        (app / "core.py").write_text("def area(w, h): return 0\n")
        failed = self.run_command("python3 -m unittest discover", cwd="app dir")
        self.assertFalse(failed.ok)
        self.assertEqual(failed.meta["exit_code"], 1)
        self.assertGreater(failed.meta["stderr_bytes"], 0)
        self.assertIn("FAILED (failures=1)", failed.content)

    def test_bounds_both_streams_preserving_start_and_finish(self):
        code = ("import sys; print('start'+'a'*120000+'end'); "
                "print('err-start'+'b'*250000+'err-end',file=sys.stderr)")
        for tool, args in [("run_python", {"code":code}), ("run_shell", {"command":f'python3 -c "{code}"'})]:
            with self.subTest(tool=tool):
                result = self.registry.dispatch(tool, args, self.ctx)
                self.assertTrue(result.ok, result.content[:80])
                for marker in ["start", "end", "err-start", "err-end"]:
                    self.assertIn(marker, result.content)
                self.assertTrue(result.meta["truncated"])
                self.assertLess(len(result.content), 8000)
                self.assertGreater(result.meta["stderr_bytes"], 249999)
                self.assertGreater(result.meta["stdout_bytes"], 119999)

    def test_timeout_reaps_the_command_tree(self):
        self.ctx.limits.tool_timeout_seconds = 0.5
        python = ("import subprocess; from pathlib import Path\n"
                  "p=subprocess.Popen(['sleep','18']); Path('child.pid').write_text(str(p.pid)); p.wait()")
        for tool, args in [("run_python", {"code":python}), ("run_shell", {"command":"sleep 18 & echo $! > child.pid; wait"})]:
            with self.subTest(tool=tool):
                before = time.monotonic()
                result = self.registry.dispatch(tool, {**args, "timeout":30}, self.ctx)
                self.assertFalse(result.ok)
                self.assertTrue(result.meta["timed_out"], result.content)
                self.assertGreater(result.meta["elapsed_ms"], 0)
                self.assertEqual(result.meta["timeout_s"], 0.5)
                self.assertLess(time.monotonic()-before, 5)
                child = int((self.root / "child.pid").read_text())
                try:
                    child_process = psutil.Process(child)
                    self.assertEqual(child_process.status(), psutil.STATUS_ZOMBIE)
                except psutil.NoSuchProcess:
                    pass

    def test_no_credentials_or_interactive_input_and_member_scope_is_retained(self):
        self.ctx.sandbox.process_env = {"MINIAGENT_MEMBER_PROCESS_SCOPE": "fixture-scope", "UNRELATED": "secret-extra"}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"fixture-secret", "SSH_AUTH_SOCK":"/fixture/fake"}):
            result = self.run_command(
                'test -z "$DEEPSEEK_API_KEY$SSH_AUTH_SOCK$UNRELATED" && '
                'test "$MINIAGENT_MEMBER_PROCESS_SCOPE" = fixture-scope && '
                'test "$HOME" = "$PWD" && ! read -r input')
        self.assertTrue(result.ok, result.content)
        self.assertNotIn("fixture-secret", result.content)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"fixture-secret", "GITHUB_TOKEN":"fixture-token"}):
            probe = self.registry.dispatch("run_python", {"code": (
                "import os,sys\nassert not any(k in os.environ for k in ('DEEPSEEK_API_KEY','GITHUB_TOKEN','UNRELATED'))\n"
                "print(os.environ['MINIAGENT_MEMBER_PROCESS_SCOPE'])\nassert sys.stdin.read()==''")}, self.ctx)
        self.assertTrue(probe.ok, probe.content)
        self.assertIn("fixture-scope", probe.content)
        python = self.run_command("""python -c 'import sys; print(sys.executable)'""")
        self.assertIn(str(Path(sys.executable).parent), python.content)

    def test_managed_background_process_available_between_tools_until_handoff(self):
        child = None
        try:
            with managed_member_processes(self.ctx.sandbox):
                started = self.run_command("sleep 12 >/dev/null 2>&1 & echo $! > service.pid")
                self.assertTrue(started.ok, started.content)
                child = psutil.Process(int((self.root / "service.pid").read_text()))
                self.assertTrue(self.run_command("kill -0 \"$(cat service.pid)\"").ok)
            self.assertFalse(child.is_running() and child.status() != psutil.STATUS_ZOMBIE)
        finally:
            if child:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass

    def test_invalid_directory_and_read_only_registry(self):
        self.assertFalse(self.run_command("pwd", cwd="..").ok)
        self.assertFalse(self.run_command("pwd", cwd="missing").ok)
        self.assertFalse(self.run_command("pwd", timeout=-1).ok)
        self.assertNotIn("run_shell", build_default_registry(read_only=True).names())

    def test_native_python_launches_and_releases_real_local_server(self):
        (self.root / "index.html").write_text("workspace-owned server")
        (self.root / "serve.py").write_text(
            "from http.server import HTTPServer, SimpleHTTPRequestHandler\n"
            "from pathlib import Path\n"
            "with HTTPServer(('127.0.0.1', 0), SimpleHTTPRequestHandler) as server:\n"
            " Path('port').write_text(str(server.server_port))\n server.serve_forever()\n")
        launch = ("import subprocess,sys\np=subprocess.Popen([sys.executable,'serve.py'], "
                  "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid)")
        get = ("import time, urllib.request\nfrom pathlib import Path\n"
               "for _ in range(50):\n if Path('port').exists(): break\n time.sleep(0.02)\n"
               "print(urllib.request.urlopen('http://127.0.0.1:'+Path('port').read_text(),timeout=2).read().decode())")
        child = None
        try:
            with managed_member_processes(self.ctx.sandbox):
                started = self.registry.dispatch("run_python", {"code":launch}, self.ctx)
                self.assertTrue(started.ok, started.content)
                child = psutil.Process(int(started.content.split("stdout:\n")[1].strip()))
                response = self.registry.dispatch("run_python", {"code":get}, self.ctx)
                self.assertTrue(response.ok, response.content)
                self.assertIn("workspace-owned server", response.content)
            self.assertFalse(child.is_running() and child.status() != psutil.STATUS_ZOMBIE)
        finally:
            if child:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass

    def test_shell_metrics_visible_in_runtime_trace(self):
        with Tracer(self.root / "trace") as tracer:
            provider = ScriptedProvider([{"tool_calls":[{"name":"run_shell","arguments":{"command":"echo done"}}]},
                                         {"text":"finished"}])
            Agent("dev", provider, self.registry, self.ctx.sandbox, Settings(), tracer=tracer).run("test")
        event = next(row for row in map(json.loads, tracer.trace_path.read_text().splitlines())
                     if row["kind"] == "tool_result")
        self.assertEqual(event["exit_code"], 0)
        self.assertEqual(event["stdout_bytes"], 5)
        self.assertEqual(event["cwd"], ".")
        self.assertGreaterEqual(event["elapsed_ms"], 0)
