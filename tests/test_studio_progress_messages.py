"""A public assistant paragraph belongs to chat even when its turn runs tools."""
import json
import threading
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.providers import ProviderError, ScriptedProvider, _collect_deepseek_stream
from miniagent.studio_service import StudioService
from miniagent.tools import ToolRegistry
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import fresh, msg, tool
from tests.test_studio_crew import roster
from tests.test_studio_service import finished, request
from tests.test_studio_superagent import message


class StudioProgressTest(unittest.TestCase):
    def test_interleaved_stream_preserves_public_text_and_keeps_private_fields_separate(self):
        def frame(delta, finish=None):
            return {"choices": [{"delta": delta, "finish_reason": finish}]}
        events = [
            frame({"content": "先核对", "reasoning_content": "private thought"}),
            frame({"tool_calls": [{"index": 0, "id": "call", "type": "function",
                                   "function": {"name": "read_file", "arguments": '{"path":'}}]}),
            frame({"content": "原始记录。", "tool_calls": [{"index": 0, "function": {"arguments": '"private.txt"}'}}]},
                  "tool_calls"),
        ]
        seen = []
        raw = _collect_deepseek_stream(iter(events), lambda *event: seen.append(event), None)
        self.assertEqual(seen, [("delta", "先核对"), ("delta", "原始记录。")])
        assistant = raw["choices"][0]["message"]
        self.assertEqual(assistant["content"], "先核对原始记录。")
        self.assertEqual(assistant["reasoning_content"], "private thought")
        self.assertIn("private.txt", assistant["tool_calls"][0]["function"]["arguments"])
        with self.assertRaises(ProviderError):
            _collect_deepseek_stream(iter(events[:2]), lambda *event: seen.append(event), None)
        self.assertEqual(seen[-1], ("discard", ""))

    def test_message_is_durable_while_tool_is_still_running_in_dm_and_assistant(self):
        root = make_temp_dir("public-progress")
        self.addCleanup(cleanup, root)
        settings = Settings(api_key="secret-in-two-chunks")
        service = StudioService(root, settings)
        self.addCleanup(service.close)
        real_dispatch = ToolRegistry.dispatch
        for kind in ("dm", "assistant"):
            with self.subTest(kind=kind):
                entered, release = threading.Event(), threading.Event()
                def blocking_write(registry, name, args, ctx):
                    if name == "write_file":
                        entered.set()
                        release.wait(5)
                    return real_dispatch(registry, name, args, ctx)
                call = tool("write_file", path="notes.txt", content="sensitive tool parameter")
                script = [{**call, "text": "正在整理记录。secret-in-two-chunks"}, {"text": "整理完毕。"}]
                with patch("miniagent.studio_service.DeepSeekProvider", return_value=ScriptedProvider(script)), \
                     patch.object(ToolRegistry, "dispatch", blocking_write):
                    run = (service.send_superagent(message("请亲自整理。")) if kind == "assistant"
                           else service.start(msg("请整理。", fresh())))
                    try:
                        self.assertTrue(entered.wait(5))
                        turn = service.detail(run["id"])["conversation"]["turns"][-1]
                        rows = [r for r in turn["events"] if r["kind"] == "assistant_progress"]
                        self.assertEqual([r["text"] for r in rows], ["正在整理记录。[REDACTED]"])
                        self.assertNotIn("sensitive tool parameter", json.dumps(rows))
                        self.assertEqual(turn["answer"], "")
                        stream = service.reply_stream(run["id"])
                        _, snapshot = stream.updates(None)
                        self.assertEqual(snapshot, [("snapshot", {"replies": []})])
                    finally:
                        release.set()
                    final = finished(service, run["id"])
                self.assertEqual(final["conversation"]["turns"][-1]["answer"], "整理完毕。")
                journal = [r for r in service.room_events(run["id"]) if r["kind"] == "assistant_progress"]
                self.assertEqual(journal, rows)

    def test_group_commit_order_crosses_delegation_without_echoing_handoffs(self):
        root = make_temp_dir("room-progress")
        self.addCleanup(cleanup, root)
        service = StudioService(root, Settings(api_key="model-key"))
        self.addCleanup(service.close)
        dispatch = tool("delegate_task", assignee="writer", objective="写下结论", remaining_roles=[],
                        deliverable_paths=["report.txt"])
        write = tool("write_file", path="report.txt", content="检查结论。")
        script = [{**dispatch, "text": "资料齐了，我请撰稿人整理结论。"},
                  {**write, "text": "我正在整理已核对的结论。"}, {"text": "结论已写入 report.txt。"},
                  {"text": "报告已交付。"}]
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=ScriptedProvider(script)):
            run = service.start(request("team", objective="整理一份结论。", conversation=roster("lead", "writer"),
                                        acceptance_files=["report.txt"]))
            detail = finished(service, run["id"])
        rows = detail["room_events"]
        milestones = [(row["kind"], row["role"]) for row in rows
                      if row["kind"] in {"assignment", "assistant_progress", "member_report"}]
        self.assertEqual(milestones, [("assistant_progress", "lead"), ("assignment", "lead"),
                                      ("assistant_progress", "writer"), ("member_report", "writer")])
        self.assertEqual(len({r["reply_id"] for r in rows if r["kind"] == "assistant_progress"}), 2)
