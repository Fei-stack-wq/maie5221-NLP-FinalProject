import json
import tempfile
import threading
import subprocess
import sys
import urllib.request
from unittest.mock import patch
import unittest
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from miniagent.checkpoint import path_for
from miniagent.server import serve
from miniagent.studio import Studio, StudioError
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_team import wait_for


class PauseResumeTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.settings = settings(Path(root.name))
        self.script = Script()
        self.provider = FakeProvider(self.script)
        self.open()
        self.gid = self.studio.create_group("编辑部", [
            {"id":"lead","name":"主编","prompt":"Edit."},
            {"id":"researcher","name":"记者","prompt":"Research."}], "lead")["id"]

    def open(self):
        self.studio = Studio(self.settings, lambda _:self.provider)
        self.addCleanup(self.studio.close)
        self.studio.restore_queue()

    def pause(self, gid, tid):
        self.assertEqual(self.studio.pause_task(tid)["status"],"pausing")
        self.studio.wait(gid,5)
        store = self.studio.group(gid)
        self.assertEqual(store.load_task(tid)["status"],"paused")
        self.assertEqual(self.studio.pause_task(tid)["status"],"paused")
        self.assertTrue(path_for(store,tid).is_file())

    def question(self, gid):
        return wait_for(lambda:(self.studio.state(gid)["live"] or {}).get("question"))

    def answer(self, gid, text):
        self.script.queues["room"]=[reply("",call("answer_open_question",quote=text))]
        self.assertEqual(self.studio.post(gid,text)["route"],"answer")

    def test_nested_member_and_pending_tool_batch_survive_restart(self):
        def member_report(messages):
            self.assertTrue(any(m.get("content","") and "Hong Kong" in m["content"] for m in messages if m["role"]=="tool"))
            self.assertTrue(any(m.get("content","") and "ISO dates" in m["content"] for m in messages if m["role"]=="user"))
            return reply("Research is ready.")
        self.script.queues.update(
            lead=[reply("",call("write_file",path="plan.md",content="Plan"),
                        call("delegate_task",assignee="researcher",task="Research and write the report."),
                        call("write_file",path="release.md",content="Released")),reply("Released"),reply("Second job")],
            researcher=[reply("",call("write_file",path="draft.md",content="Private draft"),
                              call("ask_human",question="Which campus?"),
                              call("write_file",path="result.md",content="Confirmed")),member_report])
        tid=self.studio.post(self.gid,"Prepare publication",queue=True)["task_id"]
        pending=self.question(self.gid)
        self.script.queues["room"]=[reply("",call("change_current_task",quote="ISO dates"))]
        self.studio.post(self.gid,"Use ISO dates")
        self.pause(self.gid,tid)
        store=self.studio.group(self.gid)
        self.assertTrue((store.workspace("researcher")/"draft.md").is_file())
        self.assertFalse((store.project.root/"draft.md").exists())
        second=self.studio.post(self.gid,"Second job",queue=True)["task_id"]
        self.assertEqual(store.load_task(second)["status"],"queued")
        self.studio.close()
        self.open()
        self.assertIsNone(self.studio.state(self.gid)["live"])
        self.assertEqual(self.studio.state(self.gid)["group"]["paused_task_id"],tid)
        self.studio.resume_task(tid)
        self.assertEqual(self.question(self.gid)["id"],pending["id"])
        self.studio.resume_task(tid)
        self.answer(self.gid,"Hong Kong")
        wait_for(lambda:self.studio.group(self.gid).load_task(second)["status"]=="completed")
        store=self.studio.group(self.gid)
        self.assertEqual([store.load_task(tid)[k] for k in ("status","delegations")],["completed",1])
        self.assertEqual(store.load_task(tid)["metrics"]["questions"],1)
        for name,content in (("plan.md","Plan"),("draft.md","Private draft"),("result.md","Confirmed"),("release.md","Released")):
            self.assertEqual((store.project.root/name).read_text(),content)
        events=[e["kind"] for e in store.chat() if e.get("task_id")==tid]
        for kind in ("task_started","delegation","report","question","answer","task_paused","task_resumed","task_finished"):
            self.assertEqual(events.count(kind),1,kind)
        calls=[json.loads(line) for line in (store.task_dir(tid)/"trace.jsonl").read_text().splitlines()]
        ids=[e["call_id"] for e in calls if e["type"]=="tool_call"]
        self.assertEqual((len(ids),len(set(ids))),(6,6))
        self.assertFalse(path_for(store,tid).exists())

    def test_shutdown_while_parent_waits_for_a_cross_room_dispatch(self):
        source=self.studio.assistant()["id"]
        self.script.queues.update(assistant=[reply("",call("start_task",group_id=self.gid,text="Check delivery",after_dispatch="await_result")),reply("Summary")],
                                  lead=[reply("",call("ask_human",question="Which date?")),reply("Child delivery")])
        parent=self.studio.post(source,"Commission and summarize",queue=True)["task_id"]
        q=self.question(self.gid)
        child=self.studio.tasks(self.gid)[0]["id"]
        self.studio.close()
        self.open()
        for gid,tid in ((source,parent),(self.gid,child)):
            self.assertEqual(self.studio.group(gid).load_task(tid)["status"],"paused")
        source_room=self.studio.group(source)
        self.assertFalse(any(e["kind"]=="dispatch_finished" for e in source_room.chat()))
        self.studio.resume_task(parent)
        self.studio.resume_task(child)
        self.assertEqual(self.question(self.gid)["id"],q["id"])
        self.answer(self.gid,"Tuesday")
        self.studio.wait(source,5)
        self.assertEqual(self.studio.tasks(self.gid)[0]["status"],"completed")
        self.assertEqual(len(self.studio.tasks(self.gid)),1)
        self.assertEqual(source_room.load_task(parent)["final_answer"],"Summary")
        for kind in ("dispatch_sent","dispatch_finished"):
            self.assertEqual(sum(e["kind"]==kind for e in source_room.chat()),1)

    def test_pause_between_model_decision_and_first_tool(self):
        entered,release=threading.Event(),threading.Event()
        def late(messages):
            entered.set()
            self.assertTrue(release.wait(5))
            return reply("",call("write_file",path="once.md",content="once"))
        self.script.queues["lead"]=[late,reply("finished")]
        tid=self.studio.post(self.gid,"Write once",queue=True)["task_id"]
        self.assertTrue(entered.wait(5))
        self.studio.pause_task(tid)
        release.set()
        self.studio.wait(self.gid,5)
        store=self.studio.group(self.gid)
        self.assertEqual(store.load_task(tid)["status"],"paused")
        self.assertFalse((store.workspace("lead")/"once.md").exists())
        self.script.queues["room"]=[reply("",call("resume_room_task",quote="接着做"))]
        self.studio.post(self.gid,"接着做")
        self.studio.wait(self.gid,5)
        self.assertEqual((store.project.root/"once.md").read_text(),"once")
        self.assertEqual(len(self.provider.for_agent("lead")),2)
        self.assertEqual(len(store.tasks()),1)
        with self.assertRaises(StudioError):
            self.studio.resume_task(tid)

    def test_browser_pause_restart_then_play(self):
        self.script.queues["lead"]=[reply("",call("ask_human",question="Which date?")),reply("Done")]
        tid=self.studio.post(self.gid,"Create release",queue=True)["task_id"]
        self.question(self.gid)
        servers=[]
        def start():
            main,preview=serve(self.studio,0)
            servers.extend([main,preview])
            return f"http://127.0.0.1:{main.server_port}/#{self.gid}"
        def stop():
            while servers:
                server=servers.pop()
                server.shutdown()
                server.server_close()
        self.addCleanup(stop)
        with sync_playwright() as pw:
            chrome=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            with pw.chromium.launch(**({"executable_path":str(chrome)} if chrome.exists() else {})) as browser:
                page=browser.new_page()
                page.goto(start())
                page.get_by_role("button",name="⏸ 暂停",exact=True).click()
                expect(page.get_by_role("button",name="▶ 继续执行",exact=True)).to_be_visible()
                self.studio.close()
                stop()
                self.open()
                page.goto(start())
                page.get_by_role("button",name="▶ 继续执行",exact=True).click()
                expect(page.locator("#question-banner")).to_contain_text("Which date?")
                self.answer(self.gid,"Sunday")
                expect(page.locator("#timeline")).to_contain_text("Done")
                self.assertEqual(self.studio.tasks(self.gid)[0]["id"],tid)
                self.assertEqual(self.studio.tasks(self.gid)[0]["status"],"completed")

    def test_cancel_paused_task_releases_queue_and_invalidates_checkpoint(self):
        from miniagent.studio_dispatch import cancel_task
        self.script.queues["lead"]=[reply("",call("ask_human",question="Which date?")),reply("Next job done")]
        tid=self.studio.post(self.gid,"First job",queue=True)["task_id"]
        self.question(self.gid)
        second=self.studio.post(self.gid,"Next job",queue=True)["task_id"]
        self.pause(self.gid,tid)
        store=self.studio.group(self.gid)
        self.assertEqual(cancel_task(self.studio,tid)["status"],"cancelled")
        self.studio.wait(self.gid,5)
        self.assertFalse(path_for(store,tid).exists())
        with self.assertRaises(StudioError):
            self.studio.resume_task(tid)
        self.assertEqual(store.load_task(second)["status"],"completed")

    def test_live_shell_handle_keeps_exit_result_across_pause(self):
        def wait_for_input(messages):
            from tests.helpers import tool_results
            result=next(text for text in tool_results(messages) if "Running: " in text)
            handle=result.split("Running: ")[1].split(".")[0]
            return reply("",call("ask_human",question="Continue?"),call("poll_process",process_id=handle,wait_seconds=0))
        def finished(messages):
            self.assertNotIn("Unknown process",messages[-1]["content"])
            self.assertIn("exit",messages[-1]["content"].lower())
            return reply("Collected")
        self.script.queues["lead"]=[reply("",call("run_shell",command="sleep 0.1; exit 17",yield_after_seconds=0)),wait_for_input,finished]
        tid=self.studio.post(self.gid,"Run a check",queue=True)["task_id"]
        self.question(self.gid)
        self.pause(self.gid,tid)
        self.studio.close()
        self.open()
        self.studio.resume_task(tid)
        self.question(self.gid)
        self.answer(self.gid,"Yes")
        self.studio.wait(self.gid,5)
        self.assertEqual(self.studio.group(self.gid).load_task(tid)["final_answer"],"Collected")

    def test_interrupted_stream_resumes_with_confirmed_results_and_original_step_count(self):
        from miniagent.provider_http import ProviderError
        entered,release=threading.Event(),threading.Event()
        def stream(agent,messages,tools,options):
            seen=len(self.provider.for_agent("lead"))
            if seen==1:
                return reply("",call("write_file",path="once.md",content="First result"))
            self.assertTrue(any(m["role"]=="tool" for m in messages))
            if seen==2:
                options["on_stream"]("delta","Unfinished text")
                entered.set()
                self.assertTrue(release.wait(5))
                self.assertTrue(options["should_cancel"]())
                raise ProviderError("cancelled",code="cancelled")
            return reply("Recovered")
        self.provider.respond=stream
        self.provider.supports_streaming=True
        tid=self.studio.post(self.gid,"Recover stream",queue=True)["task_id"]
        self.assertTrue(entered.wait(5))
        self.studio.pause_task(tid)
        release.set()
        self.studio.wait(self.gid,5)
        self.studio.close()
        self.open()
        self.studio.resume_task(tid)
        self.studio.wait(self.gid,5)
        store=self.studio.group(self.gid)
        record=store.load_task(tid)
        self.assertEqual((record["status"],record["metrics"]["steps"],record["metrics"]["tool_calls"]),("completed",2,1))
        self.assertNotIn("Unfinished text",json.dumps(store.chat()))
        self.assertEqual((store.project.root/"once.md").read_text(),"First result")

    def test_cli_sigterm_persists_checkpoint_for_another_process(self):
        root=self.settings.runs_dir
        (root/"offline.env").write_text("")
        self.studio.close()
        def spawn():
            proc=subprocess.Popen([sys.executable,"-c","from tests.test_pause_resume import offline_cli; offline_cli()",str(root)],
                                  stdout=subprocess.DEVNULL)
            def cleanup():
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(10)
            self.addCleanup(cleanup)
            instance=root/".studio-instance.json"
            port=wait_for(lambda:json.loads(instance.read_text())["port"] if instance.exists() else None)
            return proc, f"http://127.0.0.1:{port}"
        def api(base, path, body=None):
            headers={}
            if body is not None:
                headers={"Origin":base,"X-Studio-Token":api(base,"/bootstrap")["token"],"Content-Type":"application/json"}
            req=urllib.request.Request(base+"/api"+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
            with urllib.request.urlopen(req,timeout=5) as response:
                return json.load(response)
        proc,base=spawn()
        path="/groups/"+self.gid
        tid=api(base,path+"/messages",{"text":"Run across processes","queue":True})["task_id"]
        q=wait_for(lambda:(api(base,path)["live"] or {}).get("question"))
        proc.terminate()
        self.assertEqual(proc.wait(10),0)
        _,base=spawn()
        self.assertEqual(api(base,"/tasks/"+tid+"?view=summary")["task"]["status"],"paused")
        api(base,"/tasks/"+tid+"/resume",{})
        restored=wait_for(lambda:(api(base,path)["live"] or {}).get("question"))
        self.assertEqual(restored["id"],q["id"])
        api(base,path+"/messages",{"text":"Confirmed"})
        wait_for(lambda:api(base,"/tasks/"+tid+"?view=summary")["task"]["status"]=="completed")
        events=api(base,path)["events"]
        self.assertEqual(sum(e["kind"]=="question" for e in events),1)
        self.assertEqual([r["id"] for r in api(base,path+"/tasks")],[tid])


def offline_cli():
    from miniagent.cli import main
    def respond(agent,messages,tools,options):
        if agent=="room":
            return reply("",call("answer_open_question",quote="Confirmed"))
        if any(m["role"]=="tool" for m in messages):
            return reply("Delivered after restart.")
        return reply("",call("ask_human",question="Confirm release?"))
    with patch("miniagent.studio.deepseek_factory",lambda _:FakeProvider(respond)):
        raise SystemExit(main(["--env-file",str(Path(sys.argv[1])/"offline.env"),"--runs-dir",sys.argv[1],
                                "studio","--port","0","--no-browser"]))
