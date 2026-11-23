"""Entry-point tests for the bounded core and product features working together."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from miniagent.settings_api import save_key
from miniagent.studio import Studio, StudioError
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_team import finished, wait_for


class StudioFusionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.script = Script(room=[reply("",call("answer_open_question",quote="Keep the title."))])
        self.provider = FakeProvider(self.script)
        self.service = Studio(settings(self.root / "runs"), lambda _: self.provider)
        self.addCleanup(self.service.close)
        self.role = {"id":"dev","name":"Developer","prompt":"Maintain the project."}
        self.service.roles.save(self.role)

    def dm(self):
        return self.service.create_group("Engineering", [], "dev", role_ids=["dev"],kind="dm")["id"]

    def test_two_tasks_use_actual_published_input_and_preserve_task_snapshots(self):
        gid = self.dm()
        def report(messages):
            self.assertIn("A101",messages[-1]["content"])
            return reply("",call("write_file",path="dashboard.md",content="Bookings: A101"))
        self.script.queues["dev"] = [
            reply("",call("write_file",path="bookings.csv",content="room\nA101")),
            reply("Booking file created."),reply("",call("read_file",path="bookings.csv")),
            report,reply("",call("write_file",path="bookings.csv",content="room\nA101\nB202")),reply("Dashboard ready.")]
        first = self.service.post(gid,"Create the booking records.",queue=True)["task_id"]
        finished(self.service,gid)
        second = self.service.post(gid,"Build a dashboard from the records.",queue=True,options={"checks":[
            {"kind":"contains","path":"dashboard.md","text":"A101"},{"kind":"exists","path":"absent.md"}]})["task_id"]
        self.service.wait(gid)
        record = self.service.task_detail(second)
        self.assertEqual((record["task"]["status"],record["task"]["evaluation"]["passed"]),("completed",1))
        with zipfile.ZipFile(self.service.group(gid).task_dir(first)/"artifacts.zip") as files:
            self.assertEqual(files.namelist(),["bookings.csv"])
            self.assertEqual(files.read("bookings.csv"), b"room\nA101")
        self.assertNotIn("dashboard.md", [r["path"] for r in self.service.task_detail(first)["changes"]])
        self.assertIn("Booking file created.",self.provider.for_agent("dev")[2]["messages"][1]["content"])

    def test_approved_role_memory_crosses_rooms_only_when_explicitly_general(self):
        a,b = self.dm(),self.dm()
        self.script.queues["dev"] = [reply("",call("propose_knowledge",text="private timetable")),
            reply("",call("propose_knowledge",text="portable checklist",scope_type="general",kind="skill")),reply("Done."),
            reply("First response."),reply("Second response.")]
        self.service.post(a,"Plan the week",queue=True); self.service.wait(a)
        entries=self.service.roles.entries("dev")
        self.service.post(b,"What do you know?",queue=True); self.service.wait(b)
        initial=self.provider.for_agent("dev")[-1]["messages"][1]["content"]
        self.assertNotIn("portable checklist",initial)
        for entry in entries:
            self.service.roles.revise("dev",entry["id"],"approved")
        self.service.roles.save({**self.role,"prompt":"Updated expertise."},update=True)
        self.service.post(b,"Repeat",queue=True); self.service.wait(b)
        messages=self.provider.for_agent("dev")[-1]["messages"]
        self.assertIn("Updated expertise.",messages[0]["content"])
        self.assertIn("portable checklist",messages[1]["content"])
        self.assertNotIn("private timetable",str(messages))
        self.assertNotIn("Plan the week",str(messages))

    def test_assistant_creates_and_dispatches_with_original_text(self):
        assistant=self.service.assistant()["id"]
        text="Please  deliver\nthe memo."
        def dispatch(messages):
            gid=json.loads(messages[-1]["content"])["id"]
            return reply("",call("dispatch_task",group_id=gid,text=text))
        self.script.queues.update(assistant=[
            reply("",call("create_role",id="editor", name="Editor",prompt="Write memos.")),
            reply("",call("create_group",name="Desk",role_ids=["dev","editor"],lead="dev")),dispatch,
            reply("Created Desk and sent the memo.")],
            dev=[reply("",call("delegate_task",assignee="editor",task=text)),reply("The editor delivered memo.md.")],
            editor=[reply("",call("write_file",path="memo.md",content="Prepared.")),reply("Here is the memo.")])
        self.service.post(assistant,"Organize a desk and send the memo assignment.")
        source=self.service.group(assistant)
        receipt=next(e for e in source.chat() if e["kind"]=="dispatch_sent")
        gid=receipt["target_group_id"]
        self.service.wait(gid)
        wait_for(lambda:any(e["kind"]=="dispatch_finished" for e in source.chat()))
        data=next(e for e in source.chat() if e["kind"]=="dispatch_finished")
        self.assertEqual(data["status"],"completed")
        self.assertEqual(data["artifacts"][0]["path"],"memo.md")
        self.assertEqual(self.service.group(gid).group.role_ids, ["dev","editor"])
        self.assertEqual(self.service.group(gid).load_task(receipt["target_task_id"])["delegations"],1)
        self.assertIn(text,self.provider.for_agent("editor")[0]["messages"][1]["content"])
        self.assertEqual(self.service.group(gid).chat()[1]["text"],text)
        self.assertNotIn("dispatch_task",self.provider.for_agent("editor")[0]["tools"])

    def test_queue_steering_attachments_and_project_conflict(self):
        gid=self.dm(); local=self.root/"workspace";local.mkdir();(local/"draft.md").write_text("original")
        self.service.update_group(gid,{"folder":str(local),"pinned":True})
        self.script.queues["dev"]=[reply("",call("ask_human",question="Choose a title?")),
            reply("", call("write_file",path="draft.md",content="team version")),reply("Draft done."),
            reply("",call("copy_attachment_to_workspace",id="unused",path="copy.bin")),reply("Next done.")]
        self.service.post(gid,"Revise draft.",queue=True)
        wait_for(lambda:self.service.state(gid)["live"]["question"])
        ref=self.service.uploads.save("brief.bin",b"\x00\x11\xff")
        self.script.queues["dev"][-2]=reply("",call("copy_attachment_to_workspace",id=ref["id"],path="copy.bin"))
        queued=self.service.post(gid,"Next deliverable",queue=True,attachments=[ref],options={"thinking":"enabled"})
        self.assertEqual(queued["route"],"queued")
        (local/"draft.md").write_text("external edit")
        self.assertEqual(self.service.post(gid,"Keep the title.")["route"],"answer")
        wait_for(lambda:self.service.state(gid)["live"] is None)
        self.assertEqual((local/"draft.md").read_text(),"external edit")
        self.assertEqual((local/"draft.md.miniagent-conflict").read_text(),"team version")
        self.assertFalse((local/"draft.md.miniagent-conflict-1").exists())
        self.assertEqual((local/"copy.bin").read_bytes(),b"\x00\x11\xff")
        self.assertEqual(self.service.group(gid).load_task(queued["task_id"])["status"],"completed")
        self.assertFalse((self.service.group(gid).project.root/"draft.md.miniagent-conflict").exists())

    def test_selected_workspace_isolated_across_folder_changes_and_restart(self):
        gid=self.dm()
        store=self.service.group(gid)
        builtin=store.project.root
        (builtin/"internal.txt").write_text("Earlier room delivery")
        folders=[self.root/"Project A",self.root/"Project B"]
        for folder in folders:
            folder.mkdir()
            (folder/"brief.txt").write_text(folder.name)
            self.service.update_group(gid,{"folder":str(folder)})
            self.script.queues["dev"]=[reply("",call("read_file",path="brief.txt")),
                reply("",call("write_file",path=folder.name+".md",content=folder.name)),reply("Published.")]
            self.service.post(gid,"Deliver",queue=True)
            self.service.wait(gid)
            self.assertEqual((store.project.root/"brief.txt").read_text(),folder.name)
            self.assertFalse((store.project.root/"internal.txt").exists())
            self.assertEqual((folder/(folder.name+".md")).read_text(),folder.name)
        self.assertFalse((folders[1]/"Project A.md").exists())
        self.assertFalse((folders[0]/"Project B.md").exists())
        self.service.update_group(gid,{"folder":""})
        self.assertEqual(store.project.root,builtin)
        self.assertEqual((builtin/"internal.txt").read_text(),"Earlier room delivery")
        self.service.update_group(gid,{"folder":str(folders[0])})
        self.assertTrue((store.project.root/"Project A.md").is_file())
        self.assertFalse((store.project.root/"Project B.md").exists())
        self.service.close()
        reopened=Studio(self.service.settings,lambda _:self.provider)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.summary(reopened.group(gid))["workspace_path"],str(folders[0].resolve()))
        self.assertEqual((reopened.group(gid).project.root/"brief.txt").read_text(),"Project A")

    def test_partial_reply_is_cleared_on_provider_retry(self):
        from miniagent.studio import Running
        running = Running("t-test", "dev", None, None)
        for phase, text in [("delta","stale"), ("discard",""), ("delta","new"), ("delta"," reply")]:
            self.service._activity(running, {"type":"delta","stream_id":"one","phase":phase,"text":text})
        self.assertEqual([(e["phase"],e["text"]) for e in running.activity],
                         [("delta","stale"),("discard",""),("delta","new reply")])

    def test_settings_failure_atomic_write_and_no_key_in_response(self):
        self.service.env_file=self.root/".env"
        self.service.env_file.write_text("DEEPSEEK_MODEL=deepseek-flash\n")
        with patch("miniagent.settings_api.verify",side_effect=StudioError("rejected")):
            with self.assertRaises(StudioError):
                save_key(self.service,"fake-local-key")
        self.assertNotIn("fake-local-key",self.service.env_file.read_text())
        with patch("miniagent.settings_api.verify",return_value=None):
            result=save_key(self.service,"fake-local-key")
        self.assertIsNone(result["verified"])
        self.assertNotIn("fake-local-key",json.dumps(result))
        self.assertEqual(self.service.env_file.stat().st_mode&0o777,0o600)
        self.assertIn("DEEPSEEK_MODEL=",self.service.env_file.read_text())
        with self.assertRaises(StudioError):
            self.service.post(self.dm(),"fake-local-key")
