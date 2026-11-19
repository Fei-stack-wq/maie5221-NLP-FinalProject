"""M0-03: exact multi-round history and tool side-effect boundaries, offline."""
from __future__ import annotations

import copy
import json
import unittest
from typing import Any

from pydantic import ValidationError

from miniagent.agent import Agent
from miniagent.config import Limits, Settings
from miniagent.metrics import Tracer, load_events
from miniagent.providers import Provider
from miniagent.rao import Action, Budget, Decision, ErrorClass, RAOState, RAOUnit
from miniagent.sandbox import Sandbox
from miniagent.schemas import LLMResponse, ToolCall, ToolSpec, Usage
from miniagent.tools import build_default_registry
from tests.support import cleanup, make_temp_dir


class RecordingProvider(Provider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = iter(responses)
        self.requests: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], tools: list[ToolSpec] | None = None,
             temperature: float = 0.0) -> LLMResponse:
        self.requests.append(copy.deepcopy(messages))
        return next(self.responses)


def write_call(identity: str, path: str) -> ToolCall:
    return ToolCall(id=identity, name="write_file", arguments={"path": path, "content": "hello"})


class ResponseContractTest(unittest.TestCase):
    def test_wire_round_trip_preserves_raw_null_reasoning_and_type(self) -> None:
        raw = ' { "path" : "a.txt", "content": "\\u4f60\\n" } '
        response = LLMResponse(
            content_is_null=True, reasoning_content="private reasoning\n",
            tool_calls=[ToolCall(id="call-1", name="write_file", raw_arguments=raw)],
        )
        self.assertEqual(response.to_assistant_message(), {
            "role": "assistant", "content": None, "reasoning_content": "private reasoning\n",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {
                "name": "write_file", "arguments": raw,
            }}],
        })
        self.assertNotIn("private reasoning", repr(response))
        self.assertEqual(LLMResponse().to_assistant_message(), {"role": "assistant", "content": ""})
        self.assertEqual(LLMResponse(reasoning_content="").to_assistant_message()["reasoning_content"], "")

    def test_only_missing_raw_arguments_use_host_fallback(self) -> None:
        for raw, expected in [(None, '{"path": "中.txt"}'), ("", ""), (" ", " ")]:
            with self.subTest(raw=raw):
                response = LLMResponse(tool_calls=[ToolCall(
                    id="call", name="read_file", arguments={"path": "中.txt"}, raw_arguments=raw,
                )])
                self.assertEqual(response.to_assistant_message()["tool_calls"][0]["function"]["arguments"], expected)

    def test_protocol_fields_are_strict(self) -> None:
        for updates in [{"id": 1}, {"name": False}, {"type": "custom"}, {"raw_arguments": {}}]:
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                ToolCall.model_validate({"id": "c", "name": "read_file", **updates})
        for updates in [{"request_attempts": True}, {"request_attempts": 0},
                        {"request_attempts": 1.0}, {"content_is_null": 1},
                        {"reasoning_content": 1}, {"request_id": 1}]:
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                LLMResponse(**updates)

    def test_usage_counters_reject_negative_coerced_and_boolean_values(self) -> None:
        fields = ["prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens",
                  "prompt_cache_miss_tokens", "reasoning_tokens", "reported_total_tokens"]
        for field in fields:
            for value in [-1, True, False, "1", 1.0]:
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    Usage(**{field: value})

    def test_usage_merge_preserves_provenance_without_subtotal_double_counting(self) -> None:
        usage = Usage(prompt_tokens=10, completion_tokens=5, prompt_cache_hit_tokens=8,
                      prompt_cache_miss_tokens=2, reasoning_tokens=3, reported_total_tokens=15)
        self.assertEqual(usage.total, 15)
        self.assertEqual(Usage().merged(usage), usage)
        self.assertEqual(usage.merged(Usage()), usage)
        doubled = usage.merged(usage)
        self.assertEqual(doubled.total, 30)
        self.assertEqual(doubled.reasoning_tokens, 6)
        self.assertEqual(doubled.prompt_cache_hit_tokens, 16)
        mixed = usage.merged(Usage(prompt_tokens=2, source="estimated"))
        self.assertEqual(mixed.source, "estimated")
        self.assertIsNone(mixed.reasoning_tokens)
        unknown = mixed.merged(Usage(source="unknown"))
        self.assertEqual(unknown.source, "unknown")
        self.assertEqual(unknown.total, 17)


class RaoProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = make_temp_dir("rao-protocol")
        self.workspace = self.base / "agent"

    def tearDown(self) -> None:
        cleanup(self.base)

    def unit(self, responses: list[LLMResponse], **kwargs) -> tuple[RAOUnit, RecordingProvider]:
        provider = RecordingProvider(responses)
        unit = RAOUnit(name="worker", provider=provider, registry=build_default_registry(),
                       sandbox=Sandbox(self.workspace), limits=Limits(), **kwargs)
        return unit, provider

    def state(self) -> RAOState:
        return RAOState(goal="维护电脑", task_id="t1", history=[{"role": "user", "content": "维护电脑"}])

    def assert_complete_batch(self, state: RAOState, ids: list[str]) -> None:
        assistant = next(i for i, m in enumerate(state.history) if m.get("tool_calls"))
        following = state.history[assistant + 1:assistant + 1 + len(ids)]
        self.assertEqual([message["role"] for message in following], ["tool"] * len(ids))
        self.assertEqual([message["tool_call_id"] for message in following], ids)

    def test_a01_two_tool_rounds_then_answer_keep_full_protocol_history(self) -> None:
        raw = ' { "path" : "检查.txt", "content" : "\\u4f60好\\n" } '
        first = LLMResponse(content_is_null=True, reasoning_content="first private reasoning",
                            tool_calls=[ToolCall(id="write-1", name="write_file", raw_arguments=raw)],
                            finish_reason="tool_calls", usage=Usage(prompt_tokens=3, completion_tokens=2))
        second = LLMResponse(text="", reasoning_content="second private reasoning",
                             tool_calls=[ToolCall(id="read-2", name="read_file", raw_arguments='{"path":"检查.txt"}')],
                             finish_reason="tool_calls", usage=Usage(prompt_tokens=5, completion_tokens=2))
        final = LLMResponse(text="  已完成检查。\n", reasoning_content="final private reasoning", finish_reason="stop")
        unit, provider = self.unit([first, second, final])
        state, budget = self.state(), Budget(tokens_left=2500)
        records = [unit.step(state, budget) for _ in range(3)]
        self.assertEqual([record.decision_kind for record in records], ["act", "act", "answer"])
        self.assertEqual(records[-1].answer, "已完成检查。")
        self.assertEqual((self.workspace / "检查.txt").read_text(), "你好\n")
        self.assertEqual(provider.requests[1][1], first.to_assistant_message())
        self.assertEqual(provider.requests[1][2]["tool_call_id"], "write-1")
        self.assertEqual(provider.requests[2][:3], provider.requests[1])
        self.assertEqual(provider.requests[2][3], second.to_assistant_message())
        self.assertEqual(provider.requests[2][4]["tool_call_id"], "read-2")
        self.assertEqual(state.history[-1], final.to_assistant_message())
        self.assertNotIn("private reasoning", json.dumps([record.model_dump() for record in records]))
        self.assertEqual(budget.tokens_left, 2500 - 12)

    def test_bad_envelope_rejects_entire_batch_before_first_write(self) -> None:
        bad_calls = [write_call("", "bad.txt"), write_call("  ", "bad.txt"),
                     write_call("first", "bad.txt"),
                     write_call("second", "bad.txt").model_copy(update={"type": "custom"}),
                     write_call("second", "bad.txt").model_copy(update={"id": 12}),
                     write_call("second", "bad.txt").model_copy(update={"name": " "})]
        for bad_call in bad_calls:
            with self.subTest(call=bad_call):
                unit, _ = self.unit([LLMResponse(tool_calls=[write_call("first", "first.txt"), bad_call],
                                                finish_reason="tool_calls", usage=Usage(prompt_tokens=7))])
                state, budget = self.state(), Budget(tokens_left=2500)
                record = unit.step(state, budget)
                self.assertEqual(record.terminate_reason, ErrorClass.T_PROTOCOL)
                self.assertEqual(record.prompt_tokens, 7)
                self.assertEqual(record.tool_calls, 0)
                self.assertEqual(budget.tokens_left, 2500 - 7)
                self.assertFalse((self.workspace / "first.txt").exists())
                self.assertEqual(len(state.history), 1)

    def test_abnormal_finish_never_answers_or_executes_but_records_usage(self) -> None:
        for reason in ["length", "content_filter", "insufficient_system_resource", "aborted", "unexpected"]:
            for with_calls in [False, True]:
                with self.subTest(reason=reason, with_calls=with_calls):
                    response = LLMResponse(text="看起来完成了", finish_reason=reason,
                        tool_calls=[write_call("c1", "bad.txt")] if with_calls else [],
                        usage=Usage(prompt_tokens=7, completion_tokens=3, source="estimated"),
                        model="mock-model", request_attempts=3, request_id="req-1", reasoning_content="secret")
                    unit, _ = self.unit([response])
                    state = self.state()
                    record = unit.step(state, Budget())
                    self.assertEqual(record.terminate_reason, ErrorClass.T_PROTOCOL)
                    self.assertEqual(record.answer, "")
                    self.assertEqual(record.total_tokens, 10)
                    self.assertEqual(record.usage_source, "estimated")
                    self.assertEqual(record.finish_reason, reason)
                    self.assertEqual(record.model, "mock-model")
                    self.assertEqual(record.request_attempts, 3)
                    self.assertEqual(record.request_id, "req-1")
                    self.assertNotIn("secret", record.model_dump_json())
                    self.assertFalse((self.workspace / "bad.txt").exists())
                    if with_calls:
                        self.assert_complete_batch(state, ["c1"])
                        self.assertEqual(record.observations[-1].error_class, ErrorClass.A_SKIPPED)

    def test_inconsistent_stop_and_tool_calls_are_protocol_errors(self) -> None:
        for response in [LLMResponse(tool_calls=[write_call("c1", "bad.txt")], finish_reason="stop"),
                         LLMResponse(text="done", finish_reason="tool_calls"),
                         LLMResponse(text="not null", content_is_null=True, finish_reason="stop")]:
            with self.subTest(response=response):
                unit, _ = self.unit([response])
                record = unit.step(self.state(), Budget())
                self.assertEqual(record.terminate_reason, ErrorClass.T_PROTOCOL)
                self.assertFalse((self.workspace / "bad.txt").exists())

    def test_legacy_empty_finish_reason_still_supports_calls_and_answers(self) -> None:
        unit, _ = self.unit([LLMResponse(tool_calls=[write_call("c1", "legacy.txt")]), LLMResponse(text="done")])
        state = self.state()
        self.assertEqual(unit.step(state, Budget()).tool_calls, 1)
        self.assertEqual(unit.step(state, Budget()).answer, "done")

    def test_invalid_raw_json_remains_unchanged_and_never_executes(self) -> None:
        invalid = ["", " ", "[]", "null", "1", "{", '{"n":NaN}', '{"n":Infinity}',
                   '{"n":1e999}', '{"path":"good.txt","path":"bad.txt"}',
                   '{"nested":{"n":1,"n":2}}']
        for raw in invalid:
            with self.subTest(raw=raw):
                unit, _ = self.unit([LLMResponse(tool_calls=[ToolCall(id="bad", name="write_file",
                    arguments={"path": "bad.txt", "content": "unsafe"}, raw_arguments=raw)], finish_reason="tool_calls")])
                state = self.state()
                record = unit.step(state, Budget())
                self.assertEqual(record.error_class, ErrorClass.A_INVALID_ARGS)
                self.assertFalse((self.workspace / "bad.txt").exists())
                self.assertEqual(state.history[1]["tool_calls"][0]["function"]["arguments"], raw)
                self.assert_complete_batch(state, ["bad"])

    def test_wire_raw_arguments_are_authoritative_for_execution(self) -> None:
        raw = '{"path":"safe.txt","content":"wire content"}'
        call = ToolCall(id="c", name="write_file", raw_arguments=raw,
                        arguments={"path": "other.txt", "content": "other content"})
        unit, _ = self.unit([LLMResponse(tool_calls=[call], finish_reason="tool_calls")])
        state = self.state()
        self.assertTrue(unit.step(state, Budget()).ok)
        self.assertEqual((self.workspace / "safe.txt").read_text(), "wire content")
        self.assertFalse((self.workspace / "other.txt").exists())
        self.assertEqual(state.history[1]["tool_calls"][0]["function"]["arguments"], raw)

    def test_nested_unchecked_payload_uses_revalidated_tool_objects(self) -> None:
        response = LLMResponse(finish_reason="tool_calls").model_copy(update={
            "tool_calls": [write_call("valid", "valid.txt").model_dump()],
        })
        unit, _ = self.unit([response])
        state = self.state()
        self.assertTrue(unit.step(state, Budget()).ok)
        self.assertEqual((self.workspace / "valid.txt").read_text(), "hello")
        self.assert_complete_batch(state, ["valid"])

    def test_action_limit_skips_remaining_calls_and_closes_batch(self) -> None:
        unit, _ = self.unit([LLMResponse(tool_calls=[write_call(str(i), f"{i}.txt") for i in range(3)],
                                        finish_reason="tool_calls")], max_actions_per_step=1)
        state = self.state()
        record = unit.step(state, Budget())
        self.assertEqual([o.error_class for o in record.observations], [None, ErrorClass.A_SKIPPED, ErrorClass.A_SKIPPED])
        self.assertTrue((self.workspace / "0.txt").exists())
        self.assertFalse((self.workspace / "1.txt").exists())
        self.assertFalse((self.workspace / "2.txt").exists())
        self.assert_complete_batch(state, ["0", "1", "2"])

    def test_total_action_budget_stops_mid_batch_without_unmatched_calls(self) -> None:
        unit, _ = self.unit([LLMResponse(tool_calls=[write_call(str(i), f"{i}.txt") for i in range(3)],
                                        finish_reason="tool_calls")])
        state, budget = self.state(), Budget(actions_left=1)
        record = unit.step(state, budget)
        self.assertEqual(record.terminate_reason, ErrorClass.T_BUDGET)
        self.assertEqual(budget.actions_left, 0)
        self.assertFalse((self.workspace / "1.txt").exists())
        self.assert_complete_batch(state, ["0", "1", "2"])

    def test_abort_completes_batch_and_skips_all_later_side_effects(self) -> None:
        calls = [ToolCall(id="stop", name="abort_task", arguments={"reason": "不能继续"}),
                 write_call("later-1", "bad-1.txt"), write_call("later-2", "bad-2.txt")]
        unit, _ = self.unit([LLMResponse(tool_calls=calls, finish_reason="tool_calls")])
        state = self.state()
        record = unit.step(state, Budget())
        self.assertEqual(record.terminate_reason, ErrorClass.T_ABORT)
        self.assertEqual([o.error_class for o in record.observations], [ErrorClass.T_ABORT, ErrorClass.A_SKIPPED, ErrorClass.A_SKIPPED])
        self.assertFalse((self.workspace / "bad-1.txt").exists())
        self.assertFalse((self.workspace / "bad-2.txt").exists())
        self.assert_complete_batch(state, ["stop", "later-1", "later-2"])

    def test_loop_advisory_preserves_later_write_in_the_same_batch(self) -> None:
        calls = [ToolCall(id="list", name="list_dir", arguments={"path": "."}), write_call("later", "bad.txt")]
        unit, _ = self.unit([LLMResponse(tool_calls=calls, finish_reason="tool_calls")], loop_warn=1)
        state = self.state()
        record = unit.step(state, Budget())
        self.assertIsNone(record.terminate_reason)
        self.assertTrue((self.workspace / "bad.txt").exists())
        self.assert_complete_batch(state, ["list", "later"])

    def test_loop_warnings_follow_all_tool_results(self) -> None:
        calls = [ToolCall(id="list-1", name="list_dir", arguments={"path": "."}),
                 ToolCall(id="list-2", name="list_dir", arguments={"path": "."})]
        unit, _ = self.unit([LLMResponse(tool_calls=calls, finish_reason="tool_calls")], loop_warn=1)
        state = self.state()
        unit.step(state, Budget())
        self.assert_complete_batch(state, ["list-1", "list-2"])
        self.assertEqual([m["role"] for m in state.history], ["user", "assistant", "tool", "tool", "user", "user"])

    def test_direct_act_revalidates_later_action_before_any_write(self) -> None:
        unit, _ = self.unit([])
        decision = Decision(actions=[Action(id="valid", tool="write_file", arguments={"path": "bad.txt", "content": "x"}),
            Action(id="later", tool="read_file").model_copy(update={"type": "custom"})])
        _kind, _observations, terminate = unit.act(self.state(), decision, Budget())
        self.assertEqual(terminate, ErrorClass.T_PROTOCOL)
        self.assertFalse((self.workspace / "bad.txt").exists())

    def test_answer_acceptance_retry_preserves_reasoning_and_exact_answer_history(self) -> None:
        first = LLMResponse(text="  第一次答复\n", reasoning_content="private-answer-reasoning", finish_reason="stop")
        provider = RecordingProvider([first, LLMResponse(text="修正后的答复", finish_reason="stop")])
        outcomes = iter([(False, "检查失败"), (True, "检查通过")])
        with Tracer(self.base / "trace") as tracer:
            agent = Agent(name="worker", provider=provider, registry=build_default_registry(),
                          sandbox=Sandbox(self.workspace), settings=Settings(), tracer=tracer)
            result = agent.run("维护电脑", acceptance=lambda _root: next(outcomes))
        self.assertEqual(result.status, "completed")
        self.assertEqual(provider.requests[1][-2], first.to_assistant_message())
        self.assertEqual(provider.requests[1][-1]["role"], "user")
        self.assertIn("验收未通过", provider.requests[1][-1]["content"])
        events = load_events(self.base / "trace")
        self.assertEqual(len([event for event in events if event["kind"] == "step"]), 2)
        self.assertNotIn("private-answer-reasoning", json.dumps(events, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
