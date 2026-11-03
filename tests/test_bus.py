"""消息总线测试：订阅/广播、落盘、订阅者异常隔离。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from miniagent.bus import Message, MessageBus
from tests.support import cleanup, make_temp_dir


class BusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = make_temp_dir("bus")
        self.run_dir = self.base / "run"
        self.bus = MessageBus(self.run_dir)

    def tearDown(self) -> None:
        cleanup(self.base)

    def test_ids_are_monotonic_and_timestamped(self) -> None:
        first = self.bus.post("a", "hi")
        second = self.bus.post("b", "hello")
        self.assertEqual((first.id, second.id), (1, 2))
        self.assertGreater(first.ts, 0)
        self.assertTrue(first.iso)

    def test_subscriber_only_gets_addressed_messages(self) -> None:
        received: list[Message] = []
        self.bus.subscribe("worker", received.append)

        self.bus.post("a", "给 worker", recipients=["worker"])
        self.bus.post("b", "给 writer", recipients=["writer"])
        self.bus.post("c", "广播", recipients=["all"])

        self.assertEqual([m.content for m in received], ["给 worker", "广播"])

    def test_wildcard_subscriber_sees_everything(self) -> None:
        received: list[Message] = []
        self.bus.subscribe("*", received.append)
        self.bus.post("a", "x", recipients=["worker"])
        self.bus.post("b", "y", recipients=["writer"])
        self.assertEqual(len(received), 2)

    def test_bus_jsonl_is_persisted(self) -> None:
        self.bus.post("worker", "报告", kind="report", task_id="s1")
        path = self.run_dir / "bus.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "report")
        self.assertEqual(rows[0]["task_id"], "s1")
        self.assertEqual(rows[0]["recipients"], ["all"])

    def test_handler_exception_does_not_break_publishing(self) -> None:
        def boom(_message: Message) -> None:
            raise RuntimeError("订阅者出错")

        self.bus.subscribe("worker", boom)
        message = self.bus.post("a", "仍然要发出", recipients=["worker"])
        self.assertEqual(message.id, 1)
        self.assertEqual(self.bus.count, 1)

    def test_transcript_contains_sender_and_kind(self) -> None:
        self.bus.post("worker", "第一行", kind="report", task_id="s1")
        text = self.bus.transcript()
        self.assertIn("[report]", text)
        self.assertIn("worker", text)
        self.assertIn("s1", text)


if __name__ == "__main__":
    unittest.main()
