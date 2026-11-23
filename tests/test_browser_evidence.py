"""What QA, its lead and the room can know from one real script execution."""
import json
import tempfile
import unittest
from pathlib import Path

from miniagent.browser_evidence import Recorder, ScriptSteps, format_report
from miniagent.server import serve
from miniagent.studio import Studio
from miniagent.workbench import tool_page
from tests.helpers import FakeProvider, Script, call, reply, settings, tool_results
from tests.test_team import TEAM, finished


class BrowserEvidenceTest(unittest.TestCase):
    def test_nested_assertion_observes_each_operand_once_and_tracks_real_control_flow(self):
        code = """def check(label):
    try:
        assert sample() == label; unreachable()
        never_called()
    finally:
        cleanup()
if True:
    check('Booked')
    next_check()
else:
    unrelated()
finish_report()
"""
        steps = ScriptSteps(code)
        samples, cleanups = [], []
        def sample():
            samples.append(True)
            return "Pending"

        with tempfile.TemporaryFile() as evidence:
            evidence.truncate(steps.progress_size)
            recorder = Recorder(steps, evidence)
            try:
                try:
                    exec(steps.compile(), {steps.name: recorder, "sample": sample, "cleanup": lambda: cleanups.append(True)})
                except AssertionError as exc:
                    recorder.failure(exc, "assertion_failed")
                evidence.seek(0)
                progress = evidence.read(steps.progress_size)
                report = steps.report(progress, json.load(evidence))
            finally:
                recorder.progress.close()
        self.assertEqual(samples, [True])
        self.assertEqual(cleanups, [True])
        self.assertEqual(report["step"], {"line": 3, "source": "assert sample() == label"})
        self.assertEqual(report["observed"], ["sample() = 'Pending'", "label = 'Booked'"])
        self.assertEqual([s["source"] for s in report["unreached"]],
                         ["unreachable()", "never_called()", "next_check()", "finish_report()"])

    def test_full_unreached_list_survives_compact_model_excerpt(self):
        code = "assert False\n" + "\n".join(f"check({i})" for i in range(19))
        steps = ScriptSteps(code)
        with tempfile.TemporaryFile() as evidence:
            evidence.truncate(steps.progress_size)
            recorder = Recorder(steps, evidence)
            try:
                with self.assertRaises(AssertionError):
                    exec(steps.compile(), {steps.name: recorder})
                report = steps.report(recorder.progress, {"kind": "assertion_failed", "detail": "AssertionError"})
            finally:
                recorder.progress.close()
        self.assertEqual(report["unreached_count"], 19)
        self.assertEqual(report["unreached"][-1]["source"], "check(18)")
        self.assertIn("7 more unreached", format_report(report))

    def test_real_assertion_evidence_reaches_model_chat_log_and_browser(self):
        from playwright.sync_api import expect, sync_playwright

        code = """with (WORKSPACE / 'calls.txt').open('a') as log:
    log.write('once\\n')
print('noise' * 4000)
page.set_content('<output>Pending</output>')
expect(page.locator('output')).to_have_text('Booked', timeout=150)
expect(page.get_by_role('button', name='Cancel')).to_be_visible()
"""
        script = Script(
            lead=[reply("", call("delegate_task", assignee="qa", task="Check booking status and cancellation.")),
                  reply("Received the verification evidence.")],
            qa=[reply("", call("run_browser", code=code)),
                reply("", call("run_browser", code="assert page.locator('b').all_text_contents() == ['Room']")),
                reply("", call("run_browser", code="assert page.url == 'about:blank'")),
                reply("Checks recorded.")])
        with tempfile.TemporaryDirectory() as tmp:
            provider = FakeProvider(script)
            studio = Studio(settings(Path(tmp)), provider_factory=lambda _: provider)
            self.addCleanup(studio.close)
            gid = studio.create_group("Evidence desk", TEAM, "lead", "autonomous")["id"]
            studio.post(gid, "Verify the booking", queue=True)
            task = finished(studio, gid, timeout=30)
            store = studio.group(gid)
            results = [r for r in tool_page(store, task["id"], 0)["items"]
                       if r["type"] == "tool_result" and r["tool"] == "run_browser"]
            self.assertEqual([r["ok"] for r in results], [False, False, True])
            report = results[0]["browser_report"]
            self.assertEqual((report["kind"], report["step"]["line"]), ("assertion_failed", 5))
            self.assertEqual(report["exit_code"], 1)
            self.assertIn("Pending", " ".join(report["observed"]))
            self.assertIn("Booked", report["detail"])
            self.assertEqual(report["unreached"], [{"line": 6, "source": code.splitlines()[5]}])
            second = results[1]["browser_report"]
            self.assertIn("page.locator('b').all_text_contents() = []", second["observed"])
            self.assertNotIn("browser_report", results[2])
            member_report, = [e for e in store.chat() if e["kind"] == "report"]
            self.assertEqual(member_report["browser_checks"], {"calls": 3, "findings": [
                {"number": 1, "report": report, "call_id": results[0]["call_id"]},
                {"number": 2, "report": second, "call_id": results[1]["call_id"]}]})
            for text in (tool_results(provider.for_agent("qa")[1]["messages"])[-1],
                         tool_results(provider.for_agent("lead")[-1]["messages"])[-1]):
                for part in ("Pending", "Booked", "Not reached L6", "Cancel"):
                    self.assertIn(part, text)
            self.assertEqual(task["status"], "completed")
            self.assertEqual((store.project.root / "calls.txt").read_text(), "once\n")

            main, preview = serve(studio, 0)
            try:
                chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                with sync_playwright() as pw, pw.chromium.launch(**({"executable_path": str(chrome)} if chrome.is_file()
                                                                     else {})) as browser:
                    page = browser.new_page()
                    page.goto(f"http://127.0.0.1:{main.server_port}/#{gid}")
                    card = page.locator("#timeline .report .browser-check").first
                    card.locator("summary").click()
                    expect(card).to_contain_text("断言未通过 · 第 5 行")
                    expect(card).to_contain_text("Pending")
                    expect(card).to_contain_text("后续未执行的脚本步骤")
                    expect(card.locator("code")).to_contain_text("Cancel")
                    widget = page.locator("#timeline .tool-widget").first
                    widget.locator(":scope > summary").click()
                    row = widget.locator(".tool-row").filter(has_text="断言未通过").first
                    row.locator(":scope > summary").click()
                    row.locator(".browser-check summary").click()
                    expect(row.locator(".browser-check code")).to_be_visible()
            finally:
                for server in (main, preview):
                    server.shutdown()
                    server.server_close()
