"""Member communication through the Studio worker and actual model/tool protocol."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.agent import AgentSession
from miniagent.config import Settings, TeamLimits, fast_profile
from miniagent.providers import DeepSeekProvider
from miniagent.studio import Studio
from miniagent.team_talk import fork_history
from tests.helpers import FakeProvider, Script, call, reply, settings, tool_results
from tests.test_team import TEAM, finished, wait_for


def delegation(who, task, **options):
    return call("delegate_task", assignee=who, task=task, **options)


class TeamTalkTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def open(self, respond, **limits):
        limits = TeamLimits(retrospective=False, **limits)
        self.settings = settings(self.root, team=limits)
        self.provider = FakeProvider(respond)
        self.studio = Studio(self.settings, lambda _: self.provider)
        self.addCleanup(self.studio.close)
        self.group = self.studio.create_group("Project team", TEAM, "lead", "autonomous")["id"]
        self.store = self.studio.group(self.group)

    def start(self):
        return self.studio.post(self.group, "Complete the agreed project.", queue=True)["task_id"]

    def mail(self, kind):
        return [m for m in self.store.chat() if m["kind"] == "mail" and m["mail_kind"] == kind]

    def test_lead_adjusts_same_assignment_and_limits_survive_multiple_calls(self):
        first = call("check_in", summary="The active records use local dates.", decision="Which cutoff?", options=["UTC", "Local"])
        second = call("check_in", summary="A second request.")
        self.open(Script(lead=[reply("", delegation("dev", "Choose the correct cutoff for records.")),
                               reply('{"action":"adjust","text":"Use Hong Kong local dates."}'), reply("Delivered")],
                         dev=[reply("", first), reply("", second), reply("Used local dates.")]), check_ins=1)
        self.start()
        task = finished(self.studio, self.group)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["delegations"], 1)
        requests = self.provider.for_agent("dev")
        self.assertIn("Lead decision: adjust", tool_results(requests[1]["messages"])[-1])
        self.assertIn("Hong Kong local dates", tool_results(requests[1]["messages"])[-1])
        self.assertIn("used all 1", tool_results(requests[2]["messages"])[-1])
        checkin, = self.mail("checkin")
        decision, = self.mail("decision")
        self.assertEqual(decision["reply_to"], checkin["id"])
        self.assertEqual(task["assignments"][0]["assignment_seq"], checkin["assignment_seq"])
        self.assertEqual(task["assignments"][0]["talk"]["check_ins"], 1)
        self.assertTrue(any("Hong Kong local dates" in str(m["content"]) for m in self.provider.for_agent("lead")[-1]["messages"]
                            if m["role"] == "user"))

    def test_stop_and_report_closes_unstarted_calls_before_wrap_up(self):
        self.open(Script(lead=[reply("", delegation("dev", "Check the status and prepare a draft.")),
                               reply('{"action":"stop_and_report","text":"Keep the existing draft."}'), reply("Received")],
                         dev=[reply("", call("check_in", summary="Need a decision"),
                                    call("write_file", path="unexpected", content="should not be written")),
                              reply("Existing draft preserved.")]))
        self.start()
        task = finished(self.studio, self.group)
        request = self.provider.for_agent("dev")[-1]
        self.assertEqual(request["options"].get("tool_choice"), "none")
        DeepSeekProvider._validate_history(request["messages"], require_reasoning=False)
        self.assertFalse((self.store.project.root / "unexpected").exists())
        self.assertEqual(task["assignments"][0]["status"], "completed")

    def test_idle_peer_fork_reads_previous_work_rejects_writes_and_retains_note(self):
        self.open(Script(lead=[reply("", delegation("dev", "Publish a source summary.")),
                               reply("", delegation("qa", "Independently verify the summary.")),
                               reply("", delegation("dev", "Finish the publication handoff.")), reply("Done")],
                         dev=[reply("", call("message_teammate", member="qa", text="Inspect source.txt"),
                                       call("write_file", path="source.txt", content="Primary source")),
                              reply("The source is dated March 3."),
                              reply("", call("write_file", path="leak", content="forbidden")),
                              reply("", call("read_file", path="source.txt")), reply("Primary source, March 3."),
                              reply("Handoff delivered.")],
                         qa=[reply("", call("ask_teammate", member="qa", question="Self?"),
                                      call("ask_teammate", member="lead", question="Lead?"),
                                      call("ask_teammate", member="ghost", question="Unknown role?"),
                                      call("ask_teammate", member="dev", question="Which date?")),
                             reply("Verified March 3.")]))
        self.start()
        task = finished(self.studio, self.group)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["delegations"], 3)
        requests = self.provider.for_agent("dev")
        fork_requests = [r for r in requests if "Answer your teammate" in r["messages"][0]["content"]]
        self.assertEqual(len(fork_requests), 3)
        self.assertIn("March 3", str(fork_requests[0]["messages"]))
        self.assertNotIn("write_file", fork_requests[0]["tools"])
        self.assertIn("Unknown tool", tool_results(fork_requests[1]["messages"])[-1])
        self.assertIn("Primary source", tool_results(fork_requests[-1]["messages"])[-1])
        self.assertFalse((self.store.project.root / "leak").exists())
        self.assertFalse((self.store.workspace("dev") / "leak").exists())
        self.assertNotIn("Unknown tool", str(requests[-1]["messages"]))
        self.assertIn("Primary source, March 3.", str(requests[-1]["messages"]))
        qa = self.provider.for_agent("qa")
        self.assertIn("Inspect source.txt", str(qa[0]["messages"]))
        self.assertEqual(sum("Choose a teammate" in r for r in tool_results(qa[-1]["messages"])), 3)
        question, = self.mail("teammate_question")
        answer, = self.mail("teammate_answer")
        self.assertEqual((answer["reply_to"], answer["recipients"], answer["sender"]), (question["id"], ["qa"], "dev"))
        self.assertEqual(qa[-1]["messages"][-1]["role"], "tool")
        self.assertEqual(task["assignments"][1]["talk"]["teammate_questions"], 1)

    def test_parallel_member_keeps_working_while_its_fork_answers(self):
        working, answered = threading.Event(), threading.Event()
        def wait_in_dev(messages):
            working.set()
            if not answered.wait(5):
                raise RuntimeError("peer question did not finish during parallel assignment")
            return reply("", call("write_file", path="dev.md", content="Done"))
        def ask_during_work(messages):
            if not working.wait(5):
                raise RuntimeError("developer did not enter their assignment")
            return reply("", call("ask_teammate", member="dev", question="Where is the source?"))
        def finish_qa(messages):
            answered.set()
            return reply("", call("write_file", path="qa.md", content="Checked"))
        script = Script(default=lambda *_: reply("Reviewed shared files."),
                        lead=[reply("", delegation("dev", "Publish dev evidence.", independent=True, deliverables=["dev.md"]),
                                   delegation("qa", "Write independent QA notes.", independent=True, deliverables=["qa.md"])),
                              reply("Delivered")],
                        qa=[ask_during_work, finish_qa])
        seen = []
        def run(agent, messages, tools, options):
            if agent == "dev" and "Answer your teammate" in messages[0]["content"]:
                return reply("Look in data.csv.")
            if agent == "dev" and not working.is_set():
                return wait_in_dev(messages)
            if agent == "dev":
                seen.append(str(messages))
            return script(agent, messages, tools, options)
        self.open(run)
        self.start()
        task = finished(self.studio, self.group)
        self.assertEqual(task["delegations"], 2)
        self.assertEqual(task["metrics"]["parallel_batches"], 1)
        self.assertTrue(any("Look in data.csv." in m for m in seen))
        self.assertEqual((self.store.project.root / "dev.md").read_text(), "Done")
        question, = self.mail("teammate_question")
        self.assertFalse(any(r["kind"] == "mail_receipt" and r["mail_id"] == question["id"] for r in self.store.chat()))

    def test_elapsed_silence_fires_while_model_is_still_in_its_first_call(self):
        release, entered = threading.Event(), threading.Event()
        def busy(messages):
            entered.set()
            release.wait(4)
            return reply("", call("list_dir"))
        self.open(Script(lead=[reply("", delegation("dev", "Inspect the data.")), reply("Done")],
                         dev=[busy, reply("Done")]), silence_minutes=.003)
        self.start()
        self.assertTrue(entered.wait(4))
        try:
            wait_for(lambda: self.mail("notice"), timeout=3)
            self.assertEqual(len(self.provider.for_agent("dev")), 1)
        finally:
            release.set()
        task = finished(self.studio, self.group)
        self.assertEqual(len(self.mail("notice")), 1)
        self.assertTrue(task["assignments"][0]["talk"]["silence_prompted"])

    def test_step_silence_notices_and_fast_limits(self):
        self.open(Script(lead=[reply("", delegation("dev", "Inspect source files.", max_steps=3)), reply("Done")],
                         dev=[reply("", call("list_dir")), reply("", call("list_dir")), reply("Done")]))
        self.start()
        finished(self.studio, self.group)
        self.assertEqual(len(self.mail("notice")), 1)
        limits = fast_profile(self.settings).team
        self.assertEqual((limits.check_ins, limits.teammate_questions, limits.teammate_notes, limits.silence_minutes),
                         (1, 1, 2, 5))

    def test_communication_defaults_and_individual_environment_overrides(self):
        names = ("mailboxes", "interrupts", "lead_consult", "team_talk")
        defaults = Settings.from_env({})
        for profile in (defaults, fast_profile(defaults)):
            self.assertTrue(all(getattr(profile.team, name) for name in names))
        for disabled in names:
            with self.subTest(disabled=disabled):
                settings_with_override = Settings.from_env({"MA_" + disabled.upper(): "0"})
                for profile in (settings_with_override, fast_profile(settings_with_override)):
                    for name in names:
                        self.assertEqual(getattr(profile.team, name), name != disabled)

    def test_active_history_with_one_unanswered_call_is_valid_only_in_the_fork(self):
        batch = reply("", call("list_dir"), call("read_file", path="data"))
        header = batch.to_assistant_message()
        header["reasoning_content"] = "Need the actual data."
        answered, pending = [c.id for c in batch.tool_calls]
        source = AgentSession(messages=[{"role": "system", "content": "dev"},
                                        {"role": "user", "content": "Look up evidence."}, header,
                                        {"role": "tool", "tool_call_id": answered, "content": "data"}])
        fork = fork_history(source, settings(self.root), "read only", "dev")
        DeepSeekProvider._validate_history(fork.messages, require_reasoning=True)
        self.assertEqual(fork.messages[-1]["tool_call_id"], pending)
        self.assertIn("still running", fork.messages[-1]["content"])
        self.assertEqual(source.messages[0]["content"], "dev")
        self.assertEqual(len(source.messages), 4)

    def test_rejected_consult_closes_checkin_with_an_unavailable_receipt(self):
        self.open(Script(lead=[reply("", delegation("dev", "Check an unresolved choice.")),
                               reply("an unreadable decision"), reply("Received")],
                         dev=[reply("", call("check_in", summary="Which source?")), reply("Sent a factual report.")]))
        self.start()
        finished(self.studio, self.group)
        question, = self.mail("checkin")
        failure, = self.mail("decision")
        self.assertEqual((failure["status"], failure["reply_to"], failure["sender"]),
                         ("unavailable", question["id"], "studio"))
        self.assertIn("No lead decision", tool_results(self.provider.for_agent("dev")[-1]["messages"])[-1])

    def test_quotas_reset_on_new_assignment_without_redelegating_questions(self):
        ask = call("ask_teammate", member="qa", question="Where is the note?")
        note = call("message_teammate", member="qa", text="I will use your note.")
        self.open(Script(lead=[reply("", delegation("dev", "Find the first source.")),
                               reply("", delegation("dev", "Crosscheck the final sources.")), reply("Delivered")],
                         dev=[reply("", ask, ask, note, note), reply("First pass done"), reply("", ask, note), reply("Done")],
                         qa=[reply("notes.txt"), reply("notes.txt")]), teammate_questions=1, teammate_notes=1)
        self.start()
        task = finished(self.studio, self.group)
        self.assertEqual(len(self.mail("teammate_question")), 2)
        self.assertEqual(len(self.mail("note")), 2)
        self.assertEqual(task["delegations"], 2)
        self.assertIn("used all 1 teammate questions", str(self.provider.for_agent("dev")[1]["messages"]))
        self.assertIn("used all 1 teammate notes", str(self.provider.for_agent("dev")[1]["messages"]))
        self.assertNotEqual(task["assignments"][0]["assignment_seq"], task["assignments"][1]["assignment_seq"])

    def test_silence_deadline_is_per_assignment_and_scheduled_for_ten_minutes(self):
        entered, release = threading.Event(), threading.Event()
        def busy(messages):
            entered.set()
            release.wait(5)
            return reply("Finished.")
        self.open(Script(lead=[reply("", delegation("dev", "Review the results.")), reply("Done")], dev=[busy, reply("Finished.")]))
        with patch("miniagent.team_talk.threading.Timer") as timer:
            self.start()
            self.assertTrue(entered.wait(5))
            delay, callback = timer.call_args.args
            self.assertEqual(delay, 600)
            try:
                callback(*timer.call_args.kwargs["args"])
                callback(*timer.call_args.kwargs["args"])
            finally:
                release.set()
            finished(self.studio, self.group)
        self.assertEqual(len(self.mail("notice")), 1)
        timer.return_value.cancel.assert_called_once()

    def test_pause_discards_readonly_fork_and_resumes_the_same_question_once(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(messages):
            entered.set()
            release.wait(5)
            return reply("Discard stale answer.")
        script = Script(lead=[reply("", delegation("qa", "Verify the missing source.")), reply("Done")],
                        qa=[reply("", call("ask_teammate", member="dev", question="Which source?")), reply("Verified")],
                        dev=[delayed, reply("Fresh answer.")])
        self.open(script, teammate_questions=1)
        tid = self.start()
        self.assertTrue(entered.wait(5))
        self.studio.pause_task(tid)
        release.set()
        self.studio.wait(self.group, 6)
        self.assertEqual(self.store.load_task(tid)["status"], "paused")
        self.assertEqual(len(self.mail("teammate_question")), 1)
        self.assertEqual(len(self.mail("teammate_answer")), 0)
        self.studio.close()
        self.studio = Studio(self.settings, lambda _: self.provider)
        self.addCleanup(self.studio.close)
        self.studio.resume_task(tid)
        task = finished(self.studio, self.group)
        self.assertEqual(task["id"], tid)
        question, = self.mail("teammate_question")
        answer, = self.mail("teammate_answer")
        self.assertEqual((answer["reply_to"], answer["text"]), (question["id"], "Fresh answer."))
        self.assertEqual(task["assignments"][0]["talk"]["teammate_questions"], 1)
