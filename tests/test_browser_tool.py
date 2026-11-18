import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Limits
from miniagent.playwright_runner import _launch
from miniagent.sandbox import Sandbox
from miniagent.tools import ToolContext, build_default_registry


class BrowserLauncherTest(unittest.TestCase):
    def test_missing_browser_points_to_install_and_other_launch_errors_surface(self):
        class MissingChromium:
            executable_path = "/missing/playwright/chromium"

            def launch(self, **options):
                raise RuntimeError("Executable doesn't exist")

        with patch("miniagent.playwright_runner.shutil.which", return_value=None):
            with self.assertRaisesRegex(SystemExit, "playwright install chromium"):
                _launch(MissingChromium(), RuntimeError)
        chromium = MissingChromium()
        with patch.object(chromium, "launch", side_effect=RuntimeError("startup denied")) as launch:
            with self.assertRaisesRegex(RuntimeError, "startup denied"):
                _launch(chromium, RuntimeError)
            launch.assert_called_once()


@unittest.skipUnless(os.name == "posix", "native browser process owner requires POSIX")
class BrowserToolTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.base = Path(root.name).resolve()
        self.member = self.base / "member"
        self.member.mkdir()
        self.ctx = ToolContext(Sandbox(self.member), Limits(tool_timeout_seconds=18))
        self.registry = build_default_registry()

    def browser(self, code, **options):
        return self.registry.dispatch("run_browser", {"code": textwrap.dedent(code), **options}, self.ctx)

    def require_installed_browser(self, result):
        if any(note in result.content for note in ("环境缺少 Playwright", "环境缺少 Chromium/Chrome")):
            self.skipTest("This integration test requires Playwright and a real local Chromium/Chrome.")

    def test_registry_and_args(self):
        self.assertIn("run_browser", self.registry.names())
        self.assertNotIn("run_browser", build_default_registry(read_only=True).names())
        self.assertFalse(self.browser(" ", cwd=".").ok)
        self.assertFalse(self.browser("print(42)", cwd="../").ok)
        self.assertFalse(self.browser("pass", timeout=-1).ok)
        self.assertFalse(self.registry.dispatch("run_browser", {"url": "about:blank"}, self.ctx).ok)

    def test_real_form_on_local_http_in_member_copy(self):
        source = self.base / "original"
        source.mkdir()
        html = """<!doctype html><meta charset=utf-8><title>Booking</title>
          <label>Room<select><option>A101</option><option>B202</option></select></label>
          <label>Guest<input></label><button>Reserve</button><output></output>
          <script>document.querySelector('button').onclick=()=>{
            document.querySelector('output').value=document.querySelector('select').value+' / '+
              document.querySelector('input').value
          }</script>"""
        (source / "reserve.html").write_text(html)
        (source / "fixtures.py").write_text('GUEST = "Ada"\n')
        web = self.member / "web app"
        shutil.copytree(source, web)
        code = """
            import os, threading
            from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
            from pathlib import Path
            from playwright.sync_api import Browser, Page
            from fixtures import GUEST
            assert isinstance(page, Page) and isinstance(browser, Browser)
            assert not {'DEEPSEEK_API_KEY','GITHUB_TOKEN','SSH_AUTH_SOCK'}.intersection(os.environ)
            assert Path('reserve.html').is_file()
            with ThreadingHTTPServer(('127.0.0.1', 0), SimpleHTTPRequestHandler) as server:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    page.goto('http://127.0.0.1:%s/reserve.html' % server.server_port)
                    page.get_by_label('Room').select_option('B202')
                    page.get_by_label('Guest').fill(GUEST)
                    page.get_by_role('button', name='Reserve').click()
                    expect(page.locator('output')).to_have_text('B202 / Ada')
                    print(page.title(), page.locator('output').inner_text(), browser.version)
                finally:
                    server.shutdown()
                    thread.join()
        """
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"fixture-secret", "GITHUB_TOKEN":"fixture-github",
                                     "SSH_AUTH_SOCK":"fixture-agent"}):
            result = self.browser(code, cwd="web app")
        self.require_installed_browser(result)
        self.assertTrue(result.ok, result.content)
        self.assertEqual(result.meta["exit_code"], 0)
        self.assertEqual(result.meta["cwd"], "web app")
        self.assertGreater(result.meta["stdout_bytes"], 0)
        self.assertGreater(result.meta["elapsed_ms"], 0)
        self.assertIn("Booking B202 / Ada", result.content)
        self.assertNotIn("fixture-secret", result.content)
        self.assertEqual((source / "reserve.html").read_text(), html)
        self.assertEqual({p.name for p in web.iterdir()}, {"reserve.html", "fixtures.py"})

    def test_failing_assertion_keeps_native_error_and_runs_script_once(self):
        result = self.browser("""
            from pathlib import Path
            with Path('calls.txt').open('a') as output:
                output.write('one\\n')
            page.set_content('<button>Reserve</button>')
            expect(page.get_by_role('button')).to_have_text('Cancel', timeout=150)
        """)
        self.require_installed_browser(result)
        self.assertFalse(result.ok, result.content)
        self.assertEqual(result.meta["exit_code"], 1)
        self.assertFalse(result.meta["timed_out"])
        self.assertIn("AssertionError", result.content)
        self.assertIn("Reserve", result.content)
        self.assertEqual((self.member / "calls.txt").read_text(), "one\n")

    def test_session_cleaned_on_native_timeout(self):
        result = self.browser("""
            from pathlib import Path
            import os, time, psutil
            processes = [c for c in psutil.Process().children(recursive=True) if c.is_running()]
            assert processes, 'Playwright driver should own browser processes'
            Path('browser-pids.txt').write_text('\\n'.join(f'{c.pid} {c.create_time()}' for c in processes))
            time.sleep(20)
        """, timeout=5)
        self.require_installed_browser(result)
        self.assertTrue(result.meta.get("timed_out"), result.content)
        import psutil
        for entry in (self.member / "browser-pids.txt").read_text().splitlines():
            pid, birth = entry.split()
            try:
                proc = psutil.Process(int(pid))
                if proc.create_time() == float(birth):
                    self.assertEqual(proc.status(), psutil.STATUS_ZOMBIE, f"live browser descendant {pid}")
            except psutil.NoSuchProcess:
                pass
