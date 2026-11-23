"""Team protocol: delegation, isolation, publishing, questions, steering, budgets, continuity."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from miniagent.config import TeamLimits
from miniagent.provider_http import ProviderError
from miniagent.studio import Studio, StudioError
from tests.helpers import FakeProvider, Script, call, reply, settings, tool_results

TEAM = [
    {"id": "lead", "name": "Tech Lead", "prompt": "Coordinates delivery."},
    {"id": "dev", "name": "Developer", "prompt": "Writes code."},
    {"id": "qa", "name": "Tester", "prompt": "Tests the software."},
]


def finished(studio: Studio, group_id: str, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = studio.state(group_id)
        if state["live"] is None:
            tasks = studio.tasks(group_id)
            if tasks and tasks[-1]["status"] != "running":
                return tasks[-1]
        time.sleep(0.02)
    raise AssertionError("task did not finish")


def wait_for(predicate, timeout: float = 10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not reached")


class TeamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def studio(self, script, mode="interactive", **changes):
        provider = FakeProvider(script)
        studio = Studio(settings(self.tmp, **changes), provider_factory=lambda _s: provider)
        group = studio.create_group("Team", TEAM, "lead", mode)
        return studio, group["id"], provider

    def test_delegation_publishes_member_files_and_refreshes_the_lead(self):
        seen = {}

        def dev_writes(messages):
            seen["dev_task"] = messages[1]["content"]
            return reply("", call("write_file", path="app.py", content="print('hello')\n"))

        def dev_reports(messages):
            seen["project_during_dev"] = (studio.group(gid).project.root / "app.py").exists()
            return reply("Wrote app.py; ran it, prints hello.")

        def lead_reads(messages):
            seen["lead_delegation_result"] = tool_results(messages)[-1]
            return reply("", call("read_file", path="app.py"))

        script = Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Write app.py printing hello.",
                                 deliverables=["app.py"])),
                  lead_reads,
                  reply("", call("delegate_task", assignee="qa", task="Test app.py and report problems.")),
                  reply("Delivered app.py (prints hello); tested by qa.")],
            dev=[dev_writes, dev_reports],
            qa=[reply("", call("run_shell", command="python3 app.py")), reply("app.py prints hello. No issues.")],
        )
        studio, gid, provider = self.studio(script)
        studio.post(gid, "Build a hello app", queue=True)
        task = finished(studio, gid)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["delegations"], 2)
        store = studio.group(gid)
        self.assertEqual((store.project.root / "app.py").read_text(), "print('hello')\n")
        self.assertFalse(seen["project_during_dev"], "member work stays private until it reports")
        self.assertIn("Published to the team project: app.py (added)", seen["lead_delegation_result"])
        self.assertIn("Wrote app.py", seen["lead_delegation_result"])
        lead_read = tool_results(provider.for_agent("lead")[2]["messages"])[-1]
        self.assertIn("print('hello')", lead_read, "lead workspace was refreshed after the delegation")
        qa_run = tool_results(provider.for_agent("qa")[1]["messages"])[-1]
        self.assertIn("hello", qa_run, "qa received the published file in its own workspace")
        kinds = [e["kind"] for e in store.chat()]
        for kind in ("user_message", "task_started", "delegation", "report", "final", "task_finished"):
            self.assertIn(kind, kinds)
        self.assertEqual(store.journal("dev")[0]["files"], ["app.py"])
        self.assertIn("Write app.py printing hello.", seen["dev_task"])
        self.assertIn("delegate_task", provider.for_agent("lead")[0]["tools"])
        self.assertNotIn("delegate_task", provider.for_agent("dev")[0]["tools"])

    def test_agents_are_isolated_in_their_own_sandboxes(self):
        script = Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Try to read outside your workspace.")),
                  reply("done")],
            dev=[reply("", call("read_file", path="../lead/workspace/secret.txt")), reply("could not read it")],
        )
        studio, gid, provider = self.studio(script)
        store = studio.group(gid)
        (store.workspace("lead") / "secret.txt").write_text("lead private draft")
        studio.post(gid, "go", queue=True)
        finished(studio, gid)
        result = tool_results(provider.for_agent("dev")[1]["messages"])[-1]
        self.assertNotIn("lead private draft", result)
        self.assertIn("outside workspace", result)

    def test_question_waits_for_the_requester_answer(self):
        def lead_after_answer(messages):
            return reply("Using " + tool_results(messages)[-1].split("\n", 1)[1])
        script = Script(room=[reply("", call("answer_open_question", quote="SQLite please"))],
                        lead=[reply("", call("ask_human", question="Which database?")), lead_after_answer])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Plan the storage", queue=True)
        question = wait_for(lambda: (studio.state(gid)["live"] or {}).get("question"))
        self.assertEqual(question["question"], "Which database?")
        self.assertEqual(studio.post(gid, "SQLite please")["route"], "answer")
        task = finished(studio, gid)
        self.assertEqual(task["final_answer"], "Using SQLite please")
        self.assertEqual(task["metrics"]["questions"], 1)
        kinds = [e["kind"] for e in studio.group(gid).chat()]
        self.assertIn("question", kinds)
        self.assertIn("answer", kinds)

    def test_autonomous_mode_offers_no_question_tool(self):
        script = Script(lead=[reply("ok")])
        studio, gid, provider = self.studio(script, mode="autonomous")
        studio.post(gid, "go", queue=True)
        finished(studio, gid)
        self.assertNotIn("ask_human", provider.for_agent("lead")[0]["tools"])
        self.assertIn("not available during this task", provider.for_agent("lead")[0]["messages"][0]["content"])

    def test_message_during_a_task_reaches_the_working_member(self):
        gate = threading.Event()
        def dev_first(messages):
            gate.wait(5)
            return reply("", call("list_dir", path="."))
        def dev_second(messages):
            note = [m["content"] for m in messages if "[Message from the requester" in (m.get("content") or "")]
            return reply("noted: " + (note[0].split("\n", 1)[1] if note else "nothing"))
        script = Script(lead=[reply("", call("delegate_task", assignee="dev", task="Do the work carefully.")),
                              reply('{"action":"hold","to":[],"text":"The member received the original message."}'),
                              lambda m: reply("final: " + tool_results(m)[-1].splitlines()[2])],
                        dev=[dev_first, dev_second], room=[reply("",call("change_current_task",quote="Use Python 3.12"))])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "start", queue=True)
        wait_for(lambda: "dev" in ((studio.state(gid)["live"] or {}).get("active") or []))
        self.assertEqual(studio.post(gid, "Use Python 3.12")["route"], "steering")
        wait_for(lambda: any(e.get("mail_kind") == "decision" and e.get("action") == "hold"
                             for e in studio.group(gid).chat()))
        gate.set()
        task = finished(studio, gid)
        self.assertEqual(task["final_answer"], "final: noted: Use Python 3.12")

    def test_repeated_identical_assignments_are_refused(self):
        task_text = "Fix all the bugs in app.py and make every test pass."
        def lead(agent, messages):
            results = tool_results(messages)
            if results and results[-1].startswith("Error: Refused"):
                return reply("stopping: " + results[-1][:40])
            return reply("", call("delegate_task", assignee="dev", task=task_text))
        script = Script(default=lambda agent, m: lead(agent, m) if agent == "lead" else reply("tried, still failing"))
        studio, gid, _ = self.studio(script, team=TeamLimits(lead_max_steps=20, max_repeat_assignments=3))
        studio.post(gid, "go", queue=True)
        task = finished(studio, gid)
        self.assertEqual(task["delegations"], 3)
        self.assertTrue(task["final_answer"].startswith("stopping: Error: Refused"))

    def test_delegation_budget_is_enforced(self):
        pieces = iter(["Design the database schema for rooms and bookings.",
                       "Implement the booking HTTP endpoints with validation.",
                       "Write unit tests covering double-booking and cancellation.",
                       "Create the HTML front end with an availability grid.",
                       "Document installation and usage in README.md."])
        def lead(agent, messages):
            results = tool_results(messages)
            if results and "budget for this task" in results[-1]:
                return reply("wrapping up")
            return reply("", call("delegate_task", assignee="dev", task=next(pieces)))
        script = Script(default=lambda agent, m: lead(agent, m) if agent == "lead" else reply("did it"))
        studio, gid, _ = self.studio(script, team=TeamLimits(lead_max_steps=30, max_delegations=4))
        studio.post(gid, "go", queue=True)
        task = finished(studio, gid)
        self.assertEqual(task["delegations"], 4)
        self.assertEqual(task["final_answer"], "wrapping up")

    def test_lead_step_budget_still_produces_a_final_answer(self):
        def lead(agent, messages):
            return reply("", call("list_dir", path=".", offset=len(messages)))
        def respond(agent, messages, tools, options):
            if options.get("tool_choice") == "none":
                return reply("partial delivery report")
            return lead(agent, messages)
        provider = FakeProvider(respond)
        studio = Studio(settings(self.tmp, team=TeamLimits(lead_max_steps=4)), provider_factory=lambda _s: provider)
        gid = studio.create_group("T", TEAM, "lead")["id"]
        studio.post(gid, "go", queue=True)
        task = finished(studio, gid)
        self.assertEqual(task["status"], "incomplete")
        self.assertEqual(task["final_answer"], "partial delivery report")

    def test_member_failure_is_reported_to_the_lead(self):
        def dev(messages):
            raise ProviderError("boom", code="network_error")
        script = Script(lead=[reply("", call("delegate_task", assignee="dev", task="Write the module please.")),
                              lambda m: reply("lead saw: " + tool_results(m)[-1].splitlines()[0])],
                        dev=[dev])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "go", queue=True)
        task = finished(studio, gid)
        self.assertEqual(task["status"], "completed")
        self.assertIn("status: error", task["final_answer"])

    def test_mention_routes_to_the_member_directly(self):
        script = Script(qa=[reply("checked")])
        studio, gid, provider = self.studio(script)
        studio.post(gid, "@qa please check the app", queue=True)
        task = finished(studio, gid)
        self.assertEqual(task["primary"], "qa")
        request = provider.for_agent("qa")[0]
        self.assertNotIn("delegate_task", request["tools"])
        self.assertIn("addressed this message to you directly", request["messages"][0]["content"])
        self.assertTrue(request["messages"][1]["content"].endswith("please check the app"))
        with self.assertRaises(StudioError):
            studio.post(gid, "@nobody hi")

    def test_second_task_sees_the_first_task_and_the_project(self):
        script = Script(lead=[reply("", call("write_file", path="booking.py", content="# v1\n")),
                              reply("FINAL-ONE: booking.py created"),
                              reply("second done")])
        studio, gid, provider = self.studio(script)
        studio.post(gid, "Task one: build booking", queue=True)
        finished(studio, gid)
        studio.post(gid, "Task two: add a dashboard", queue=True)
        finished(studio, gid)
        second = provider.for_agent("lead")[-1]["messages"][1]["content"]
        self.assertIn("Task one: build booking", second)
        self.assertIn("FINAL-ONE", second)
        self.assertIn("booking.py", second)
        self.assertTrue(second.endswith("Task two: add a dashboard"))

    def test_cancel_while_waiting_for_an_answer(self):
        script = Script(lead=[reply("", call("ask_human", question="Proceed?"))],
                        default=lambda a, m: reply("unreachable"))
        studio, gid, _ = self.studio(script)
        studio.post(gid, "go", queue=True)
        wait_for(lambda: (studio.state(gid)["live"] or {}).get("question"))
        studio.cancel(gid)
        task = finished(studio, gid)
        self.assertEqual(task["status"], "cancelled")

    def test_memory_note_is_shown_in_later_assignments(self):
        script = Script(lead=[reply("", call("propose_knowledge", text="Requester prefers SQLite.")), reply("ok"),
                              reply("second")])
        studio, gid, provider = self.studio(script)
        studio.post(gid, "one", queue=True)
        finished(studio, gid)
        note, = studio.roles.entries("lead")
        self.assertEqual(note["status"], "proposed")
        self.assertEqual(studio.roles.entries("lead", group=gid), [])
        studio.roles.revise("lead",note["id"],"approved")
        studio.post(gid, "two", queue=True)
        finished(studio, gid)
        self.assertIn("Requester prefers SQLite.", provider.for_agent("lead")[-1]["messages"][1]["content"])


if __name__ == "__main__":
    unittest.main()
