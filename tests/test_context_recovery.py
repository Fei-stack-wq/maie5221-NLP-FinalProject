"""Transport classification, genuine shrink-and-retry, and visible usage accounting."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import Mock, patch

from miniagent.acceptance import accept_files
from miniagent.agent import Agent
from miniagent.config import Settings
from miniagent.metrics import Tracer, load_events
from miniagent.provider_http import ProviderError, post_json
from miniagent.providers import DeepSeekProvider
from miniagent.request_window import fit_history
from miniagent.sandbox import Sandbox
from miniagent.tools import build_default_registry
from tests.test_harness_runtime import response, write


class StubDeepSeek(DeepSeekProvider):
    def __init__(self, script):
        super().__init__("test-only")
        self.script = iter(script)
        self.calls = []

    def chat(self, messages, tools=None, temperature=0.0, **kwargs):
        self.calls.append((messages, kwargs["max_output_tokens"]))
        item = next(self.script)
        if isinstance(item, Exception):
            raise item
        return item


class ModelCapacityRecoveryTest(unittest.TestCase):
    def test_context_code_only_and_sanitized_http_error(self):
        url = "https://api.deepseek.com/chat/completions"
        for status, body, expected in (
                (400, {"error":{"code":"context_length_exceeded","message":"private123"}}, "context_length_exceeded"),
                (422, {"error":{"message":"This model's maximum context length is 1000000. private123"}}, "context_length_exceeded"),
                (400, {"error":{"message":"Invalid max_tokens private123"}}, "http_error"),
                (413, {"error":{"message":"Request body too large private123"}}, "http_error")):
            with self.subTest(status=status, expected=expected):
                opener = Mock()
                opener.open.side_effect = HTTPError(url, status, "private123", {}, io.BytesIO(json.dumps(body).encode()))
                with patch("miniagent.provider_http.urllib.request.build_opener", return_value=opener):
                    with self.assertRaises(ProviderError) as error:
                        post_json(url, {}, api_key="test-token", max_retries=2)
                self.assertEqual(error.exception.code, expected)
                self.assertNotIn("private123", str(error.exception))
                self.assertEqual(opener.open.call_count, 1)

    def test_server_overflow_retries_smaller_envelope_without_reexecuting_a_tool(self):
        overflow = ProviderError("overflow", code="context_length_exceeded", status_code=400, attempts=1)
        provider = StubDeepSeek([response(write("first", "result.txt", "proof")), overflow, response(text="done")])
        with tempfile.TemporaryDirectory() as tmp, Tracer(Path(tmp) / "trace") as trace:
            agent = Agent("worker", provider, build_default_registry(), Sandbox(Path(tmp)/"files"), Settings(), tracer=trace)
            result = agent.run("write result", acceptance=accept_files("result.txt", min_bytes=5, contains={"result.txt":"proof"}))
            self.assertEqual((result.status, result.tool_calls), ("completed", 1), result.error)
            self.assertLess(provider.calls[2][1], provider.calls[1][1])
            self.assertEqual(1, sum(e.get("recovery") == "shrink_and_retry" for e in load_events(Path(tmp)/"trace")))

    def test_generic_provider_failure_and_second_overflow_stop(self):
        overflow = ProviderError("overflow", code="context_length_exceeded", status_code=422, attempts=1)
        for failures, calls in (([ProviderError("bad parameters", code="http_error")], 1), ([overflow, overflow], 2)):
            with self.subTest(calls=calls), tempfile.TemporaryDirectory() as root:
                provider = StubDeepSeek(failures)
                agent = Agent("worker", provider, build_default_registry(), Sandbox(root), Settings())
                result = agent.run("inspect the project")
                self.assertEqual(result.status, "error")
                self.assertEqual(len(provider.calls), calls)
                self.assertEqual(result.tool_calls, 0)

    def test_large_latest_tool_result_and_user_answer_survive_as_paired_evidence(self):
        def batch(name, cid, output):
            call = {"id":cid,"type":"function","function":{"name":name,"arguments":"{}"}}
            return [{"role":"assistant","content":"","tool_calls":[call]},
                    {"role":"tool","tool_call_id":cid,"content":output}]
        receipt = "FIRST-" + "x"*90000 + "-LAST"
        human = "human decision"
        history = [{"role":"user","content":"maintain project"}, *batch("ask_human","human",human),
                   *batch("read_file","read",receipt)]
        view, pruned = fit_history(history, window=12000, ceiling=10000, available=None, tool_schemas=[])
        self.assertTrue(pruned)
        self.assertEqual(view.messages[2]["content"], human)
        self.assertEqual(view.messages[-1]["tool_call_id"], "read")
        self.assertTrue(view.messages[-1]["content"].startswith("FIRST-"))
        self.assertTrue(view.messages[-1]["content"].endswith("-LAST"))
        self.assertEqual(history[-1]["content"], receipt)
