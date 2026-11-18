import io
import threading
import time
import unittest

from miniagent.human import TerminalHuman
from miniagent.rao import Budget, ErrorClass
from tests.test_rao import _unit, _state
from tests.support import cleanup, make_temp_dir
from tests import test_studio_discussion as discussion


class TerminalReadinessTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("human-readiness")
        self.addCleanup(cleanup, self.root)

    def test_auto_approve_is_not_a_substitute_for_question_answer(self):
        events = []
        human = TerminalHuman(stdin=io.StringIO("real requirement\n"), stdout=io.StringIO(), auto_approve=True,
                              emit=lambda kind, **data: events.append({"kind": kind, **data}))
        self.assertTrue(human.approve("remove file"))
        self.assertEqual(human.ask("Which goal?"), "real requirement")
        self.assertEqual(events[1]["source"], "policy")
        self.assertEqual(events[3]["source"], "stdin")
        self.assertIsNone(human.ask("missing"))

    def test_missing_or_empty_reply_stops_the_batch_before_write(self):
        for reply in (None, "", "  "):
            unit = _unit(self.root, [{"tool_calls": [
                {"name": "ask_human", "arguments": {"question": "Required preference?"}},
                {"name": "write_file", "arguments": {"path": "after.txt", "content": "must not happen"}}]}],
                human_handler=lambda _: reply)
            result = unit.step(_state(), Budget())
            self.assertEqual(result.terminate_reason, ErrorClass.T_HUMAN_UNAVAILABLE)
            self.assertFalse((self.root / "after.txt").exists())

    def test_delete_approval_is_bound_to_the_reviewed_file_version(self):
        from miniagent.tools import ToolContext, build_default_registry
        from miniagent.config import Limits
        from miniagent.sandbox import Sandbox
        candidate = self.root / "candidate.txt"
        candidate.write_text("reviewed version")
        def approve(prompt):
            self.assertIn("sha256=", prompt)
            candidate.write_text("new version written during approval")
            return True
        result = build_default_registry().dispatch("delete_file", {"path": "candidate.txt"},
                    ToolContext(Sandbox(self.root), Limits(), approve=approve))
        self.assertFalse(result.ok)
        self.assertEqual(candidate.read_text(), "new version written during approval")

    def test_blocked_human_and_tool_receive_remaining_deadline(self):
        released = threading.Event()
        self.addCleanup(released.set)
        unit = _unit(self.root, [{"tool_calls": [{"name": "ask_human", "arguments": {"question": "wait"}}]}],
                     human_handler=lambda _: released.wait(5))
        started = time.monotonic()
        unit.step(_state(), Budget(seconds_left=.08))
        self.assertLess(time.monotonic() - started, 1)
        # A real process cannot take its configured 20 seconds when the task
        # has only a fraction of a second remaining.
        unit = _unit(self.root, [{"tool_calls": [{"name": "run_python", "arguments": {"code": "import time; time.sleep(4)"}}]}])
        started = time.monotonic()
        record = unit.step(_state(), Budget(seconds_left=.2))
        self.assertFalse(record.ok)
        self.assertLess(time.monotonic() - started, 2)


class ApprovalHTTPTest(unittest.TestCase):
    setUp = discussion.DiscussionHTTPTest.setUp
    get = discussion.DiscussionHTTPTest.get
    exchange = discussion.DiscussionHTTPTest.exchange
    start_chat = discussion.DiscussionHTTPTest.start_chat
    detail_at = discussion.DiscussionHTTPTest.detail_at

    def test_allow_and_deny_are_explicit_audited_and_control_real_delete(self):
        from tests.test_studio_conversations import tool
        for answer, decision in (("批准", "allow"), ("拒绝", "deny")):
            identity = self.start_chat([
                tool("write_file", path="candidate.txt", content="preserve until approved"),
                tool("delete_file", path="candidate.txt"),
                tool("write_file", path="receipt.txt", content="The explicit human decision was processed."), {"text": "done"}],
                acceptance_files=["receipt.txt"])
            base = "/api/runs/" + identity
            waiting = self.detail_at(identity, {"waiting_user"})
            question = waiting["pending_questions"][0]
            self.assertEqual(question["request_kind"], "approval")
            self.assertEqual(len(question["action_digest"]), 64)
            self.assertEqual(self.exchange("POST", base + "/questions/" + question["request_id"], {"answer": "maybe"})[0], 400)
            self.assertEqual(self.get(base)["run"]["status"], "waiting_user")
            self.assertEqual(self.exchange("POST", base + "/questions/" + question["request_id"], {"answer": answer})[0], 200)
            done = self.detail_at(identity, {"completed"})
            names = [a["name"] for a in done["artifacts"]]
            self.assertEqual("candidate.txt" in names, decision == "deny")
            event = next(e for e in done["room_events"] if e["kind"] == "human_answer")
            self.assertEqual(event["decision"], decision)
            self.assertEqual(event["action_digest"], question["action_digest"])
            self.assertTrue(done["human_timing"]["measurement_complete"])
            self.assertGreater(done["human_timing"]["waiting_seconds"], 0)

    def test_cancelled_approval_does_not_delete_or_accept_late_answer(self):
        from tests.test_studio_conversations import tool
        identity = self.start_chat([tool("write_file", path="candidate.txt", content="preserve"),
                                    tool("delete_file", path="candidate.txt"), {"text": "unused"}])
        base = "/api/runs/" + identity
        q = self.detail_at(identity, {"waiting_user"})["pending_questions"][0]
        self.exchange("POST", base + "/cancel", {})
        done = self.detail_at(identity, {"cancelled"})
        self.assertIn("candidate.txt", [a["name"] for a in done["artifacts"]])
        self.assertEqual(self.exchange("POST", base + "/questions/" + q["request_id"], {"answer": "批准"})[0], 409)
