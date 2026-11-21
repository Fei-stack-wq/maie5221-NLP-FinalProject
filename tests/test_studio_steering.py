"""Mid-execution user input over real Studio workers with a scripted model."""
import io
import threading
import unittest
import uuid
from unittest.mock import patch

from miniagent.acceptance import evaluate
from miniagent.config import Settings
from miniagent.studio_service import StudioError, StudioService
from miniagent.studio_uploads import save_upload
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, fresh, msg, tool
from tests.test_studio_crew import roster
from tests.test_studio_service import finished, request


def correction(text, **fields):
    return {"request_id": str(uuid.uuid4()), "message": text, **fields}


class Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait(self):
        self.entered.set()
        if not self.release.wait(10):
            raise AssertionError("fixture did not release the worker")


class PausedModel(RecordingProvider):
    def __init__(self, script, pause_at=0):
        super().__init__(script)
        self.gate, self.pause_at = Gate(), pause_at

    def chat(self, messages, tools=None, temperature=0):
        if len(self.requests) == self.pause_at:
            self.gate.wait()
        return super().chat(messages, tools, temperature)


class StudioSteeringTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("chat-steer")
        self.addCleanup(cleanup, self.root)
        self.service = StudioService(self.root, Settings(api_key="fixture-key"))
        self.addCleanup(self.service.close)

    def test_stale_tool_batch_is_paired_but_never_executed_and_first_live_attachment_is_readable(self):
        ref = save_upload(self.root, "new.txt", io.BytesIO(b"file from chat"), 14)
        model = PausedModel([tool("write_file", path="obsolete.txt", content="old"),
                             tool("read_attachment", id=ref["id"]), {"text": "Updated result."}])
        update = correction("Use this document instead.", attachments=[ref["id"]])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
            run = self.service.send_superagent(correction("Inspect the material."))["id"]
            try:
                self.assertTrue(model.gate.entered.wait(5))
                saved = self.service.steer(run, update)
                self.assertEqual(self.service.steer(run, update), saved)
                with self.assertRaises(StudioError) as replay:
                    self.service.steer(run, {**update, "message": "Different content"})
                self.assertEqual(replay.exception.code, "steer_conflict")
            finally:
                model.gate.release.set()
            detail = finished(self.service, run)
        self.assertEqual(self.service.steer(run, update), saved)
        with self.assertRaises(StudioError) as ended:
            self.service.steer(run, correction("Too late for this turn"))
        self.assertEqual(ended.exception.code, "run_finished")
        self.assertNotIn("read_attachment", model.requests[0][1])
        self.assertIn("read_attachment", model.requests[1][1])
        self.assertIn("file from chat", str(model.requests[2][0]))
        messages = model.requests[1][0]
        index = next(i for i, message in enumerate(messages) if message.get("tool_calls"))
        self.assertEqual(messages[index]["tool_calls"][0]["id"], messages[index + 1]["tool_call_id"])
        self.assertIn(update["message"], messages[index + 2]["content"])
        self.assertFalse((self.root / run / "agents/worker/obsolete.txt").exists())
        steer, = detail["conversation"]["turns"][0]["steering"]
        self.assertEqual((steer["seen_by"], steer["message_attachments"]), ("worker", [ref]))

    def test_message_during_verification_rejects_an_already_generated_answer(self):
        gate, model = Gate(), RecordingProvider([{"text": "Old answer."}, {"text": "New answer."}])
        def verify(*args, **kwargs):
            if not gate.entered.is_set():
                gate.wait()
            return evaluate(*args, **kwargs)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model), patch("miniagent.agent.evaluate", verify):
            run = self.service.start(msg("Write an answer.", fresh()))["id"]
            try:
                self.assertTrue(gate.entered.wait(5))
                self.service.steer(run, correction("Please answer in English."))
            finally:
                gate.release.set()
            detail = finished(self.service, run)
        turn, = detail["conversation"]["turns"]
        self.assertEqual(turn["answer"], "New answer.")
        self.assertIn("Please answer in English.", str(model.requests[-1][0]))

    def test_withdrawn_dispatch_is_not_started_and_can_be_replaced_idempotently(self):
        model = PausedModel([{"text": "Original."}, {"text": "Revised."}])
        target = {"target_kind": "role", "target_id": "reviewer", "objective": "Review the inventory."}
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
            run = self.service.send_superagent(correction("Arrange a review."))["id"]
            try:
                self.assertTrue(model.gate.entered.wait(5))
                original = self.service.request_studio_dispatch(run, **target)
                self.service.steer(run, correction("Change the deliverable."))
                cancelled = self.service.cancel_queued_dispatch(run, original["dispatch_id"])
                self.assertEqual(cancelled["status"], "cancelled")
                replacement = self.service.request_studio_dispatch(run, **target, acceptance_files=["notes.md"])
                replay = self.service.request_studio_dispatch(run, **target, acceptance_files=["notes.md"])
                self.assertNotEqual(original["dispatch_id"], replacement["dispatch_id"])
                self.assertEqual(replay["dispatch_id"], replacement["dispatch_id"])
                self.service.cancel_queued_dispatch(run, replacement["dispatch_id"])
            finally:
                model.gate.release.set()
            finished(self.service, run)
        self.assertEqual([r["status"] for r in self.service.studio_dispatches(run)], ["cancelled", "cancelled"])

    def test_failed_provider_does_not_acknowledge_a_queued_message(self):
        gate = Gate()
        model = RecordingProvider([])
        def fail(*args, **kwargs):
            gate.wait()
            raise OSError("offline fixture")
        with patch.object(model, "chat", side_effect=fail), patch(
                "miniagent.studio_service.DeepSeekProvider", return_value=model):
            run = self.service.start(msg("Check current availability.", fresh()))["id"]
            try:
                self.assertTrue(gate.entered.wait(5))
                self.service.steer(run, correction("Include the afternoon."))
            finally:
                gate.release.set()
            detail = finished(self.service, run)
        row, = detail["conversation"]["turns"][0]["steering"]
        self.assertEqual((row["text"], row["seen_by"]), ("Include the afternoon.", None))

    def test_group_member_and_coordinator_both_receive_a_mid_assignment_correction(self):
        note = "Please include the revised inventory."
        model = PausedModel([tool("delegate_task", assignee="writer", objective="Write inventory",
                                  remaining_roles=[], deliverable_paths=["list.md"]),
                             {"text": "Obsolete handoff."},
                             tool("write_file", path="list.md", content="Revised inventory."),
                             {"text": "Revised inventory delivered."}, {"text": "Final inventory delivered."}], pause_at=1)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
            run = self.service.start(request("team", objective="Prepare the inventory", conversation=roster("lead", "writer"),
                                             acceptance_files=["list.md"]))["id"]
            try:
                self.assertTrue(model.gate.entered.wait(5))
                self.service.steer(run, correction(note))
            finally:
                model.gate.release.set()
            detail = finished(self.service, run)
        self.assertEqual(detail["run"]["status"], "completed", detail.get("diagnostics"))
        self.assertIn(note, str(model.requests[2][0]))
        self.assertIn(note, str(model.requests[-1][0]))
        self.assertEqual(detail["conversation"]["turns"][0]["steering"][0]["seen_by"], "writer")

    def test_active_question_wakes_without_treating_steer_as_an_approval(self):
        model = RecordingProvider([tool("ask_human", question="Which package should I use?", decision_kind="user_preference"),
                                   {"text": "Discussing the local option."}])
        waiting = threading.Event()
        original = self.service._record_room_event
        def observe(run, role, kind, text, **fields):
            event = original(run, role, kind, text, **fields)
            if kind == "human_question":
                waiting.set()
            return event
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model), patch.object(
                self.service, "_record_room_event", side_effect=observe):
            run = self.service.start(msg("Plan installation.", fresh()))["id"]
            try:
                self.assertTrue(waiting.wait(5))
                self.service.steer(run, correction("Change of plan: just list the local options."))
                detail = finished(self.service, run)
            finally:
                self.service.cancel(run)
        turn, = detail["conversation"]["turns"]
        self.assertIn("question_closed", [event["kind"] for event in turn["events"]])
        self.assertNotIn("human_answer", [event["kind"] for event in turn["events"]])
        self.assertIn("just list the local options", str(model.requests[-1][0]))
