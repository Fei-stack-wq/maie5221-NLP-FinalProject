"""Drive the read-only workbench through HTTP and Chromium on independent listeners."""
import json
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from miniagent.server import serve
from miniagent.studio import Studio
from miniagent.workbench import tool_page
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_server import request
from tests.test_team import TEAM, finished


class WorkbenchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        script = Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Write the guide.")),
                  reply("", call("delegate_task", assignee="dev", task="Add the appendix.")),
                  reply("Delivered docs/guide.md.")],
            dev=[reply("", call("read_file", path="missing.txt")),
                 reply("", call("write_file", path="docs/guide.md", content="# First\n[Appendix](../appendix.md#details)")),
                 reply("Guide ready."),
                 reply("", call("write_file", path="appendix.md", content="# Appendix\n## Details\n<script>window.bad=true</script>")),
                 reply("Appendix ready."),
                 reply("", call("write_file", path="docs/guide.md", content="# Second")),
                 reply("Updated docs/guide.md.")],
            room=[reply("The first task delivered a guide. See docs/guide.md.")],
        )
        cls.studio = Studio(settings(Path(cls.tmp.name)), lambda s: FakeProvider(script))
        cls.gid = cls.studio.create_group("Docs team", TEAM, "lead")["id"]
        cls.studio.post(cls.gid, "Deliver a guide", queue=True)
        cls.first = finished(cls.studio, cls.gid)["id"]
        cls.studio.post(cls.gid, "@dev Update the guide", queue=True)
        cls.studio.wait(cls.gid, 10)
        cls.second = cls.studio.tasks(cls.gid)[-1]["id"]
        cls.server, cls.preview = serve(cls.studio, 0)
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.studio.close()
        for server in (cls.server, cls.preview):
            server.shutdown()
            server.server_close()
        cls.tmp.cleanup()

    def test_versioned_http_and_pre_snapshot_history(self):
        first = self.base + "/api/tasks/" + self.first + "/files/"
        latest = self.base + "/api/tasks/" + self.second + "/files/"
        status, headers, body = request(first + "docs/guide.md")
        self.assertEqual(status, 200)
        self.assertIn(b"# First", body)
        self.assertEqual(headers["Content-Security-Policy"], "sandbox")
        self.assertEqual(request(latest + "docs/guide.md")[2], b"# Second")
        self.assertEqual(request(latest + "appendix.md")[0], 404)
        self.assertEqual(request(first + "docs/guide.md?sha256=" + "0"*64)[0], 404)
        self.assertIn(request(first + "%2e%2e%2ftask.json")[0], (400, 404))
        archive = self.studio.group(self.gid).task_dir(self.first) / "artifacts.zip"
        archive.rename(archive.with_suffix(".bak"))
        try:
            self.assertEqual(request(first + "docs/guide.md")[0], 404)
            self.assertIn(b"Details", request(first + "appendix.md")[2])
        finally:
            archive.with_suffix(".bak").rename(archive)

    def test_live_tool_resolves_in_place_after_answer(self):
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
            script = Script(lead=[reply("", call("ask_human", question="Which colour?")), reply("Blue it is.")],
                            room=[reply("", call("answer_open_question", quote="Blue"))])
            studio = Studio(settings(Path(tmp)), lambda s: FakeProvider(script))
            main, preview = serve(studio, 0)
            try:
                gid = studio.create_group("Live", TEAM, "lead")["id"]
                studio.post(gid, "Pick a colour", queue=True)
                chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                with pw.chromium.launch(**({"executable_path":str(chrome)} if chrome.exists() else {})) as browser:
                    page = browser.new_page()
                    page.goto(f"http://127.0.0.1:{main.server_port}/#{gid}")
                    widget = page.locator(".chat-run .tool-widget")
                    widget.locator(":scope > summary").click()
                    row = widget.locator(".tool-row")
                    row.locator("summary").click()
                    expect(row.locator(".tool-badge")).to_have_text("等待结果")
                    page.locator("#input").fill("Blue")
                    page.locator("#composer button[type=submit]").click()
                    expect(row.locator(".tool-badge")).to_have_text("完成")
                    expect(row.locator(".tool-output")).to_contain_text("Blue")
                    expect(row).to_have_count(1)
                    expect(page.locator(".msg.final")).to_contain_text("Blue it is.")
            finally:
                studio.close()
                for server in (main, preview):
                    server.shutdown()
                    server.server_close()

    def test_trace_cursor_incomplete_line_and_reused_id(self):
        store = self.studio.group(self.gid)
        tid = "test-cursor"
        trace = store.task_dir(tid) / "trace.jsonl"
        pair = [{"type":kind, "agent":"dev", "call_id":"same"} for kind in ("tool_call", "tool_result")]
        trace.write_bytes(b'{"type":"step"}\n'*512 + json.dumps(pair[0]).encode() + b"\n{")
        skipped = tool_page(store, tid, 0)
        self.assertEqual(skipped["items"], [])
        self.assertTrue(skipped["more"])
        first = tool_page(store, tid, skipped["cursor"])
        self.assertEqual(len(first["items"]), 1)
        with trace.open("ab") as stream:
            stream.write(json.dumps(pair[1]).encode()[1:] + b"\n")
            for row in pair:
                stream.write(json.dumps(row).encode() + b"\n")
        tail = tool_page(store, tid, first["cursor"])
        self.assertEqual([e["type"] for e in tail["items"]], ["tool_result", "tool_call", "tool_result"])
        self.assertEqual(tool_page(store, tid, tail["cursor"])["items"], [])

    def test_browser_history_tree_tools_cards_and_reader(self):
        with sync_playwright() as pw:
            chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            with pw.chromium.launch(**({"executable_path":str(chrome)} if chrome.exists() else {})) as browser:
                page = browser.new_page(viewport={"width":1280, "height":850})
                errors, mutations = [], []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("request", lambda r: mutations.append(r.url) if r.method not in ("HEAD", "GET") else None)
                page.goto(self.base + "/#workbench")
                expect(page.get_by_role("heading", name="工作台")).to_be_visible()
                page.locator(".bench-row").filter(has_text="Deliver a guide").click()
                expect(page.locator(".branch")).to_have_count(2)
                expect(page.locator(".tree-actor")).to_have_text(["@lead → @dev", "@lead → @dev"])
                page.locator(".branch summary").nth(1).click()
                expect(page.locator(".branch[open]")).to_contain_text("Appendix ready.")
                page.get_by_role("button", name="打开会话").click()
                first_card = page.locator("#timeline .artifact-card").filter(has_text="docs/guide.md").first
                expect(first_card).to_be_visible()
                first_card.click()
                expect(page.locator("#reader-body h1")).to_have_text("First")
                page.get_by_role("link", name="Appendix").click()
                expect(page.locator("#reader-body h2")).to_have_text("Details")
                self.assertFalse(page.evaluate("Boolean(window.bad)"))
                with page.expect_download() as download:
                    page.locator("#reader-download").click()
                self.assertIn(b"## Details", Path(download.value.path()).read_bytes())
                page.locator("#reader-back").click()
                expect(page.locator("#reader-body h1")).to_have_text("First")
                page.locator("#reader-close").click()
                page.locator(".msg.final").first.get_by_role("link", name="docs/guide.md").click()
                expect(page.locator("#reader-body h1")).to_have_text("First")
                page.locator("#reader-close").click()
                history = page.locator(".chat-run .tool-widget").first
                history.locator(":scope > summary").click()
                failed = history.locator(".tool-row").filter(has_text="read_file")
                failed.locator("summary").click()
                expect(failed.locator(".tool-badge")).to_have_text("失败")
                expect(failed).to_contain_text("missing.txt")
                expect(failed.locator(".tool-output")).not_to_be_empty()
                page.set_viewport_size({"width":390,"height":844})
                page.locator("#timeline .artifact-card").filter(has_text="docs/guide.md").last.click()
                expect(page.locator("#reader-body h1")).to_have_text("Second")
                self.assertLessEqual(page.locator("#document-reader").bounding_box()["width"], 390)
                page.locator("#reader-close").click()
                page.locator(".chat-run").last.get_by_role("button",name="查看任务树").click()
                expect(page.locator(".tree-root")).to_contain_text("@dev")
                expect(page.locator(".branch")).to_have_count(0)
                page.locator("#sheet-close").click()
                page.locator("#workbench-nav").click()
                page.reload()
                expect(page.locator(".bench-row")).to_have_count(2)
                self.assertEqual(mutations, [])
                self.assertEqual(errors, [])
                # Exercise the actual composer, including the room's conversation router.
                page.get_by_label("发送到会话").select_option(self.gid)
                page.get_by_label("消息", exact=True).fill("What did the first task do?")
                page.get_by_role("button", name="发送到会话").click()
                expect(page.locator("#timeline")).to_contain_text("The first task delivered a guide.")
                self.assertEqual(len(self.studio.tasks(self.gid)), 2)
                page.locator("#timeline .msg").last.get_by_role("link", name="docs/guide.md").click()
                expect(page.locator("#reader-origin")).to_have_text("选择交付版本")
                page.get_by_role("button", name="打开任务 " + self.first).click()
                expect(page.locator("#reader-body h1")).to_have_text("First")
