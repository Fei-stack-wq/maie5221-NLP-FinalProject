"""Actual browser, local HTTP and the scripted model. No provider credential is required."""
import tempfile
import unittest
from pathlib import Path
from miniagent.server import serve
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings


class StudioBrowserTest(unittest.TestCase):
    def test_assistant_conversation_changes_existing_roster_without_creating_tasks(self):
        from playwright.sync_api import sync_playwright, expect
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
            script = Script()
            studio = Studio(settings(Path(tmp)), lambda _:FakeProvider(script))
            studio.roles.save({"id":"reviewer","name":"校对","prompt":"校对材料"})
            team = studio.create_group("文档组",[{"id":"editor","name":"编辑","prompt":"写材料"}],"editor")["id"]
            assistant = studio.assistant()["id"]
            text = "把校对拉进文档组"
            script.queues["assistant"] = [reply("工作室有编辑和校对。"),
                reply("",call("change_group_members",group_id=team,add_role_ids=["reviewer"])),
                reply("文档组现在有编辑、校对。"),
                reply("",call("change_group_members",group_id=team,remove_member_ids=["reviewer"])),
                reply("已把校对移出文档组。")]
            main,preview = serve(studio,0)
            try:
                chrome=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                with pw.chromium.launch(**({"executable_path":str(chrome)} if chrome.exists() else {})) as browser:
                    page=browser.new_page()
                    base=f"http://127.0.0.1:{main.server_port}/"
                    page.goto(base+"#"+assistant)
                    def send(message):
                        page.locator("#input").fill(message)
                        page.locator("#composer button[type=submit]").click()
                    send("我们有哪些角色？")
                    expect(page.locator("#timeline")).to_contain_text("工作室有编辑和校对。")
                    send(text)
                    expect(page.locator("#timeline")).to_contain_text("编辑、校对")
                    group_page = browser.new_page()
                    group_page.goto(base+"#"+team)
                    expect(group_page.locator("#room-members")).to_contain_text("@reviewer")
                    expect(group_page.locator("#timeline")).to_contain_text("校对 加入群聊")
                    send("从文档组移出校对")
                    expect(group_page.locator("#timeline")).to_contain_text("校对 已移出群聊")
                    expect(group_page.locator("#room-members")).not_to_contain_text("@reviewer")
                    self.assertEqual(studio.tasks(assistant),[])
                    self.assertEqual(studio.tasks(team),[])
            finally:
                studio.close()
                for server in (main,preview):
                    server.shutdown()
                    server.server_close()

    def test_create_role_open_private_chat_and_get_delivery(self):
        from playwright.sync_api import sync_playwright, expect
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
            script=Script(room=[reply("",call("start_room_task",quote="Deliver a memo")),reply("上轮交付了 note.md。")], editor=[reply("",call("write_file",path="note.md",content="# Hello")),reply("Finished the memo.")])
            studio=Studio(settings(Path(tmp)),lambda s:FakeProvider(script))
            main,preview=serve(studio,0)
            try:
                chrome=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                with pw.chromium.launch(**({"executable_path":str(chrome)} if chrome.exists() else {})) as browser:
                    page=browser.new_page()
                    errors=[]
                    page.on("pageerror",lambda error:errors.append(str(error)))
                    page.goto("http://127.0.0.1:"+str(main.server_port))
                    expect(page.locator("#room-name")).to_contain_text("工作室助手")
                    page.get_by_role("button",name="角色库",exact=True).click()
                    page.get_by_role("button",name="创建角色",exact=True).click()
                    page.get_by_label("角色 ID",exact=True).fill("editor")
                    page.get_by_label("名称",exact=True).fill("编辑")
                    page.get_by_label("职责",exact=True).fill("Write memos.")
                    page.get_by_role("button",name="保存",exact=True).click()
                    page.get_by_role("button",name="编辑 · @editor").click()
                    page.get_by_role("button",name="添加知识",exact=True).click()
                    page.get_by_label("内容",exact=True).fill("Check the exported file.")
                    page.get_by_role("button",name="保存草稿",exact=True).click()
                    page.get_by_role("button",name="批准",exact=True).click()
                    expect(page.locator("#sheet")).to_contain_text("approved")
                    page.get_by_role("button",name="新建私聊",exact=True).click()
                    page.locator("#input").fill("Deliver a memo")
                    page.locator("#composer button[type=submit]").click()
                    expect(page.locator("#timeline")).to_contain_text("Finished the memo.",timeout=10000)
                    before_count = len(studio.tasks(page.url.split("#")[-1]))
                    page.locator("#input").fill("你都做了啥")
                    page.locator("#composer button[type=submit]").click()
                    expect(page.locator("#timeline")).to_contain_text("上轮交付了 note.md。")
                    self.assertEqual(len(studio.tasks(page.url.split("#")[-1])), before_count)
                    page.locator("#toggle-files").click()
                    page.locator('[data-path="note.md"]').click()
                    expect(page.locator("#reader-body h1")).to_have_text("Hello")
                    page.locator("#reader-close").click()
                    page.set_viewport_size({"width":390,"height":844})
                    page.locator("#close-files").click()
                    page.locator("#tasks-nav").click()
                    page.get_by_role("button",name="完成 · 编辑 · Deliver a memo").click()
                    page.get_by_role("button",name="产物",exact=True).click()
                    expect(page.locator("#sheet .artifact-card")).to_contain_text("note.md")
                    page.locator("#sheet-close").click()
                    page.set_viewport_size({"width":1280,"height":800})
                    page.locator("#new-group").click()
                    page.locator("#group-name").fill("Editing desk")
                    page.locator("#library-member").select_option("editor")
                    page.locator("#save-group").click()
                    expect(page.locator("#member-panel")).to_be_visible()
                    expect(page.locator("#room-members")).to_contain_text("@editor")
                    page.locator("#manage-members").click()
                    page.locator(".member-row").filter(has=page.locator(".m-id[value=editor]")).get_by_role("button",name="移除").click()
                    page.locator("#save-group").click()
                    expect(page.locator("#room-members")).not_to_contain_text("@editor")
                    self.assertEqual(errors,[])
            finally:
                studio.close()
                for server in (main,preview):
                    server.shutdown()
                    server.server_close()
