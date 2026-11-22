"""Independent work waits durably; conversational replies never occupy a worker."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.studio_service import StudioService
from tests.test_room_reply import output
from tests.test_studio_service import finished
from tests.test_studio_steering import PausedModel


def message(parent, text, *, work=True):
    return {"request_id": str(uuid.uuid4()), "text": text, "parent_run_id": parent,
            "intent": "work" if work else "auto", "options": {"mode": "single"} if work else {}}


class GroupQueueTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Settings(api_key="fixture-key")
        self.repo = Path(tmp.name) / "runs"
        self.service = self.open()

    def open(self):
        service = StudioService(self.repo, self.settings, max_workers=1)
        self.addCleanup(service.close)
        return service

    def group(self, name):
        return self.service.create_channel({"request_id": str(uuid.uuid4()), "name": name,
                                            "lead": "worker", "members": ["worker"]})["id"]

    def await_started(self, service, room, identity):
        until = time.monotonic() + 6
        while time.monotonic() < until:
            row = next(row for row in service.group_messages(room) if row["request_id"] == identity)
            if row.get("linked_run_id"):
                return row["linked_run_id"]
            time.sleep(0.02)
        self.fail("Message still waiting for dispatch: " + str(row))

    def test_free_slot_dispatches_fifo_and_concurrent_questions_leave_work_alone(self):
        group = self.group("Queue")
        model = PausedModel([{"text": "First done."}])
        self.addCleanup(model.gate.release.set)
        replies = [model, ScriptedProvider([{"text": "Second done."}]), ScriptedProvider([{"text": "Third done."}])]
        with patch("miniagent.studio_service.DeepSeekProvider", side_effect=replies):
            run = self.service.send_group_message(group, message(group, "First job"))["run"]["id"]
            self.assertTrue(model.gate.entered.wait(4))
            try:
                second, third = message(run, "Run the second independent job"), message(run, "Run a third job")
                second["intent"] = "auto"
                # Another Studio process sees the live owner of this room. An automatic
                # semantic decision to start unrelated work must not steer that owner.
                other = self.open()
                with patch("miniagent.studio_room_reply.DeepSeekProvider.chat",
                           return_value=output(action="work", quote=second["text"])):
                    response = other.send_group_message(group, second)
                self.assertEqual(response["message"]["queue_state"], "queued")
                self.assertNotIn("run", response)
                response = self.service.send_group_message(group, third)
                self.assertEqual(response["message"]["queue_state"], "queued")
                with patch("miniagent.studio_room_reply.decide_room_message", return_value={
                        "action": "reply", "text": "The first job is in progress."}):
                    question = self.service.send_group_message(group, message(run, "What is happening?", work=False))
                self.assertEqual(question["message"]["disposition"], "reply")
                self.assertEqual(len(self.service.list_runs()["runs"]), 1)
                self.assertFalse(self.service._steering[run].rows)
            finally:
                model.gate.release.set()
            end = self.await_started(self.service, group, third["request_id"])
            finished(self.service, end)
        turns = self.service.detail(end)["conversation"]["turns"]
        self.assertEqual([turn["user"] for turn in turns], ["First job", second["text"], third["text"]])
        self.assertEqual(self.service.send_group_message(group, third)["run"]["id"], end)
        self.assertEqual(len(self.service.list_runs()["runs"]), 3)

    def test_release_wakes_another_room_after_global_slot_contention(self):
        room_a, room_b = self.group("Busy"), self.group("Waiting for slot")
        model = PausedModel([{"text": "Released slot."}])
        self.addCleanup(model.gate.release.set)
        with patch("miniagent.studio_service.DeepSeekProvider", side_effect=[model, ScriptedProvider([{"text": "Done."}])]):
            self.service.send_group_message(room_a, message(room_a, "Occupy the only worker"))
            self.assertTrue(model.gate.entered.wait(4))
            item = message(room_b, "Make a separate report")
            try:
                self.assertEqual(self.service.send_group_message(room_b, item)["message"]["queue_state"], "queued")
            finally:
                model.gate.release.set()
            target = self.await_started(self.service, room_b, item["request_id"])
            finished(self.service, target)
        self.assertEqual(self.service.detail(target)["conversation"]["id"], room_b)

    def test_restart_replays_prepared_payload_after_task_created_before_receipt(self):
        room = self.group("Crash replay")
        item = message(room, "Publish once")
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=ScriptedProvider([{"text": "Done."}])):
            run = self.service.send_group_message(room, item)["run"]["id"]
            finished(self.service, run)
        self.service.close()
        with closing(sqlite3.connect(self.repo / room / "room-messages.sqlite3")) as db, db:
            # Crash boundary: start() durably created the task, but the message
            # linkage transaction has not yet happened.
            payload = {"request_id": str(uuid.uuid5(uuid.NAMESPACE_URL, room + "/messages/" + item["request_id"])),
                       "mode": "single", "objective": item["text"],
                       "conversation": {"intent": "edit", "parent_run_id": room, "recipient": "worker"}}
            db.execute("INSERT INTO work_queue VALUES (?,?,?)", (item["request_id"], "{}", json.dumps(payload)))
            raw, = db.execute("SELECT message FROM messages WHERE request_id=?", (item["request_id"],)).fetchone()
            old = json.loads(raw)
            old.pop("linked_run_id")
            old.update(queue_state="queued")
            db.execute("UPDATE messages SET message=? WHERE request_id=?", (json.dumps(old), item["request_id"]))
        with patch("miniagent.studio_service.DeepSeekProvider", side_effect=AssertionError("replay relaunched worker")):
            restarted = self.open()
            self.assertEqual(self.await_started(restarted, room, item["request_id"]), run)
            self.assertEqual(len(restarted.list_runs()["runs"]), 1)
