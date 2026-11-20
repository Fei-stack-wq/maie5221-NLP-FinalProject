import os
import gzip
import hashlib
import shutil
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Limits
from miniagent.crew_progress import _observed_browser_files
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

    def test_loopback_document_receipt_requires_actual_served_bytes_and_dom_assertion(self):
        html = b'<label for="startSelect">Start</label><select id="startSelect"><option>09:00</option></select>'
        (self.member / "booking.html").write_bytes(html)
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type","text/html")
                self.send_header("Content-Encoding","gzip")
                self.end_headers()
                self.wfile.write(gzip.compress(html))
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
        serving = threading.Thread(target=server.serve_forever,daemon=True)
        serving.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/reverse-proxy/booking"
            start = f"from pathlib import Path\naddress = {url!r}\nresponse = page.goto(address)\n"
            identity = "assert response.body() == Path('booking.html').read_bytes()\n"
            dom = "assert page.get_by_label('Start').input_value() == '09:00'"
            # The real assertion can live in a helper/try block and can use
            # a locator alias. The test has no need to parse the response body.
            code = start + """def verify():
    form = page.get_by_label('Start')
    expect(form).to_have_value('09:00')
try:
    verify()
finally:
    pass
"""
            digest = hashlib.sha256(html).hexdigest()
            actual = {"booking.html":digest, "wrong.html":"1"*64}
            for checks in (code, start + dom):
                result = self.browser(checks)
                self.require_installed_browser(result)
                self.assertTrue(result.ok, result.content)
                self.assertEqual(result.meta["asserted_browser_documents"], [digest])
                self.assertEqual(_observed_browser_files(result.meta, actual), {"booking.html"})
                self.assertFalse(_observed_browser_files(result.meta, {"booking.html":"0"*64}))
            for incomplete in (start + identity + "assert response.status == 200",
                               start + "def never_called():\n    expect(page.get_by_label('Start')).to_be_visible()",
                               start + "assert True or page.get_by_label('Start').count()",
                               start + "page.set_content('<p>replacement</p>')\nexpect(page.locator('p')).to_be_visible()",
                               start + "other = context.new_page()\nother.set_content('<p>other</p>')\nexpect(other.locator('p')).to_be_visible()"):
                result = self.browser(incomplete)
                self.assertTrue(result.ok, result.content)
                self.assertNotIn("asserted_browser_documents",result.meta)
        finally:
            server.shutdown()
            server.server_close()
            serving.join()

    def test_file_navigation_attributes_the_executed_assertion_to_its_exact_local_path(self):
        html = b"<label>Guest<input></label>"
        (self.member / "booking.html").write_bytes(html)
        (self.member / "other.html").write_bytes(html)
        start = "from pathlib import Path\npage.goto(Path('booking.html').resolve().as_uri())\n"
        result = self.browser(start + "expect(page.get_by_label('Guest')).to_be_editable()")
        self.require_installed_browser(result)
        self.assertTrue(result.ok, result.content)
        digest = hashlib.sha256(html).hexdigest()
        self.assertEqual(result.meta.get("asserted_browser_files"), {"booking.html":digest})
        self.assertEqual(_observed_browser_files(result.meta,{"booking.html":digest,"other.html":digest}),
                         {"booking.html"})

    def test_script_written_receipt_after_closed_page_cannot_attest_a_document(self):
        result = self.browser("""
            import os
            from pathlib import Path
            page.set_content('<h1>Made up</h1>')
            assert page.title() == ''
            page.close()
            (Path(os.environ['HOME']) / 'checked-document.sha256').write_bytes(b'0'*96)
        """)
        self.require_installed_browser(result)
        self.assertTrue(result.ok,result.content)
        self.assertNotIn("loopback_document_sha256",result.meta)

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
