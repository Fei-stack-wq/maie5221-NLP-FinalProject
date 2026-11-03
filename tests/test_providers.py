"""Provider 测试：响应解析是最容易埋雷的地方（模型返回的东西不总是合法的）。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

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
                "choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}],
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
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c1",
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
                            "tool_calls": [
                                {"id": "c1", "function": {"name": "read_file", "arguments": "{oops"}}
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


if __name__ == "__main__":
    unittest.main()
