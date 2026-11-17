"""Provider 测试：响应解析是最容易埋雷的地方（模型返回的东西不总是合法的）。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch

from miniagent.provider_http import HTTPResult
from miniagent.schemas import ToolSpec

from miniagent.providers import DeepSeekProvider, ProviderError, ScriptedProvider
from tests.support import cleanup, make_temp_dir


class ScriptedProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root: Path = make_temp_dir("provider")

    def tearDown(self) -> None:
        cleanup(self.root)

    def test_reads_script_with_bom(self) -> None:
        """Windows 编辑器写出的 UTF-8 BOM 不该让剧本读不出来。"""
        path = self.root / "script.json"
        path.write_text(
            json.dumps([{"text": "完成"}], ensure_ascii=False), encoding="utf-8-sig"
        )
        provider = ScriptedProvider.from_json(path)
        self.assertEqual(provider.chat([]).text, "完成")

    def test_exhausted_script_raises(self) -> None:
        """剧本耗尽必须报错 —— 假装成功会让完成率虚高。"""
        provider = ScriptedProvider([{"tool_calls": [{"name": "list_dir", "arguments": {}}]}])
        provider.chat([])
        with self.assertRaises(ProviderError):
            provider.chat([])


class DeepSeekParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = DeepSeekProvider(api_key="test-key")

    def test_parses_text_and_usage(self) -> None:
        response = self.provider._parse(
            {
                "model": "deepseek-flash",
                "choices": [{"message": {"role": "assistant", "content": "你好"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            },
            latency_ms=42.0,
        )
        self.assertEqual(response.text, "你好")
        self.assertEqual(response.usage.total, 15)
        self.assertEqual(response.finish_reason, "stop")

    def test_parses_tool_calls(self) -> None:
        response = self.provider._parse(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c1", "type": "function",
                                    "function": {"name": "read_file", "arguments": '{"path": "a.md"}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            latency_ms=1.0,
        )
        self.assertEqual(len(response.tool_calls), 1)
        call = response.tool_calls[0]
        self.assertEqual(call.name, "read_file")
        self.assertEqual(call.arguments, {"path": "a.md"})
        self.assertIsNone(call.parse_error)

    def test_malformed_arguments_become_parse_error_not_crash(self) -> None:
        response = self.provider._parse(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{oops"}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            latency_ms=1.0,
        )
        call = response.tool_calls[0]
        self.assertIsNotNone(call.parse_error)
        self.assertEqual(call.arguments, {})
        self.assertEqual(call.raw_arguments, "{oops")

    def test_missing_choices_raises(self) -> None:
        with self.assertRaises(ProviderError):
            self.provider._parse({"error": "boom"}, latency_ms=0.0)

    def test_empty_key_is_rejected_early(self) -> None:
        with self.assertRaises(ProviderError):
            DeepSeekProvider(api_key="")


def envelope(*, calls=None, finish="stop", reasoning=None, content="ok"):
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if calls is not None:
        message["tool_calls"] = calls
    return {"id": "req-1", "model": "deepseek-flash",
            "choices": [{"message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}}


def tool_call(raw='{"path":"a.md"}', identity="call-1"):
    return {"id": identity, "type": "function",
            "function": {"name": "read_file", "arguments": raw}}


class DeepSeekProtocolTest(unittest.TestCase):
    def setUp(self):
        self.provider = DeepSeekProvider("test-key")
        self.tools = [ToolSpec(name="read_file", description="read",
                               parameters={"type": "object", "properties": {"path": {"type": "string"}}})]
        self.messages = [{"role": "user", "content": "read a file"}]

    def test_payload_zero_temperature_explicit_and_input_unchanged(self):
        before = deepcopy(self.messages)
        payload = self.provider._build_payload(self.messages, self.tools, 0.0)
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["tool_choice"], "auto")
        payload["messages"][0]["content"] = "mutated"
        self.assertEqual(self.messages, before)

    def test_thinking_payload_preserves_full_history_and_raw_arguments(self):
        provider = DeepSeekProvider("test-key", thinking="enabled", reasoning_effort="max")
        response = provider._parse(envelope(calls=[tool_call(' { "path" : "a.md" } ')],
                                           finish="tool_calls", reasoning="private-r", content=None), 0)
        history = self.messages + [response.to_assistant_message(),
                                   {"role": "tool", "tool_call_id": "call-1", "content": "result"},
                                   {"role": "assistant", "content": "done", "reasoning_content": "previous-final"},
                                   {"role": "user", "content": "read again"}]
        payload = provider._build_payload(history, self.tools, 0.0)
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertIsNone(payload["messages"][1]["content"])
        self.assertEqual(payload["messages"][1]["reasoning_content"], "private-r")
        self.assertEqual(payload["messages"][1]["tool_calls"][0]["function"]["arguments"], ' { "path" : "a.md" } ')
        self.assertEqual(payload["messages"][3]["reasoning_content"], "previous-final")
        del history[3]["reasoning_content"]
        with self.assertRaises(ProviderError):
            provider._build_payload(history, self.tools, 0)

    def test_tool_batch_must_close_before_user_or_next_request(self):
        assistant = {"role": "assistant", "content": None,
                     "tool_calls": [tool_call(), tool_call(identity="call-2")]}
        result = {"role": "tool", "tool_call_id": "call-1", "content": "ok"}
        for tail in ([], [result], [result, result],
                     [result, {"role": "user", "content": "warning"}],
                     [{"role": "tool", "tool_call_id": "orphan", "content": "ok"}]):
            with self.subTest(tail=tail), self.assertRaises(ProviderError):
                self.provider._build_payload(self.messages + [assistant] + tail, self.tools, 0)
        self.provider._build_payload(self.messages + [assistant, result,
            {"role": "tool", "tool_call_id": "call-2", "content": "ok"}], self.tools, 0)

    def test_invalid_json_arguments_preserve_raw_without_executing(self):
        for raw in ("", "{oops", "[]", "null", '{"x":1,"x":2}',
                    '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}'):
            with self.subTest(raw=raw):
                response = self.provider._parse(envelope(calls=[tool_call(raw)], finish="tool_calls"), 0)
                call = response.tool_calls[0]
                self.assertIsNotNone(call.parse_error)
                self.assertEqual(call.raw_arguments, raw)
                self.assertEqual(response.to_assistant_message()["tool_calls"][0]["function"]["arguments"], raw)

    def test_bad_tool_envelopes_fail_closed(self):
        for field, value in (("id", ""), ("id", None), ("type", "other"),
                             ("function", {}), ("function", {"name": "read_file", "arguments": {}})):
            call = tool_call(); call[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ProviderError):
                self.provider._parse(envelope(calls=[call], finish="tool_calls"), 0)
        with self.assertRaises(ProviderError):
            self.provider._parse(envelope(calls=[tool_call(), tool_call()], finish="tool_calls"), 0)

    def test_finish_reason_and_envelope_validation(self):
        fixtures = [envelope(finish="unknown"), envelope(finish=None), envelope(finish="tool_calls"),
                    envelope(calls=[tool_call()]), envelope(content=42), {"choices": []}]
        missing = envelope(); del missing["choices"][0]["message"]["content"]; fixtures.append(missing)
        wrongrole = envelope(); wrongrole["choices"][0]["message"]["role"]="user"; fixtures.append(wrongrole)
        for fixture in fixtures:
            with self.subTest(fixture=fixture), self.assertRaises(ProviderError):
                self.provider._parse(fixture, 0)
        for reason in ("length", "content_filter", "insufficient_system_resource", "aborted"):
            self.assertEqual(self.provider._parse(envelope(finish=reason), 0).finish_reason, reason)

    def test_usage_is_reported_unknown_or_invalid_without_subset_double_counting(self):
        fixture = envelope()
        fixture["usage"].update(prompt_cache_hit_tokens=10, prompt_cache_miss_tokens=2,
                                completion_tokens_details={"reasoning_tokens": 2})
        usage = self.provider._parse(fixture, 0).usage
        self.assertEqual(usage.total, 15)
        self.assertEqual(usage.reasoning_tokens, 2)
        self.assertEqual(usage.source, "reported")
        del fixture["usage"]
        self.assertEqual(self.provider._parse(fixture, 0).usage.source, "unknown")
        for usage in ({}, {"prompt_tokens": True, "completion_tokens": 0},
                      {"prompt_tokens": -1, "completion_tokens": 3},
                      {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 99},
                      {"prompt_tokens": 12, "completion_tokens": 3, "prompt_cache_hit_tokens": 13},
                      {"prompt_tokens": 12, "completion_tokens": 3, "completion_tokens_details": {"reasoning_tokens": 4}}):
            fixture["usage"] = usage
            with self.subTest(usage=usage), self.assertRaises(ProviderError):
                self.provider._parse(fixture, 0)

    @patch("miniagent.providers.post_json")
    def test_chat_propagates_options_attempts_and_private_reasoning(self, post):
        provider = DeepSeekProvider("test-key", thinking="enabled", timeout=7, deadline_seconds=19, max_retries=1)
        post.return_value = HTTPResult(envelope(reasoning="private-secret"), attempts=2, latency_ms=10)
        response = provider.chat(self.messages, self.tools)
        self.assertEqual(response.request_attempts, 2)
        self.assertEqual(response.request_id, "req-1")
        self.assertNotIn("private-secret", repr(response))
        self.assertEqual(post.call_args.kwargs["timeout"], 7)
        self.assertEqual(post.call_args.kwargs["deadline_seconds"], 19)
        self.assertEqual(post.call_args.kwargs["max_retries"], 1)
        post.return_value = HTTPResult(envelope(), attempts=2, latency_ms=10)
        with self.assertRaises(ProviderError) as raised:
            provider.chat(self.messages, self.tools)
        self.assertEqual(raised.exception.attempts, 2)
        self.assertNotIn("test-key", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
