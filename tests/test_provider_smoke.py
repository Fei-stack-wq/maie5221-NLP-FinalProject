"""Protocol-probe fixtures exercise real history round trips without networking."""
from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from miniagent.provider_http import HTTPResult
from miniagent.provider_smoke import run_provider_smoke
from miniagent.providers import DeepSeekProvider, Provider, ProviderError
from miniagent.schemas import LLMResponse, ToolCall, Usage


_PRIVATE_REASONING = "private-reasoning-sentinel-never-public"
_SECRET = "private-api-key-sentinel-never-public"


class ProbeProvider(Provider):
    """Respond to actual tool results rather than a precomputed success script."""

    model = "deepseek-flash"
    thinking = "disabled"
    api_key = _SECRET

    def __init__(self, mutation=None, thinking="disabled"):
        self.histories = []
        self.tools_seen = []
        self.temperatures = []
        self.token = None
        self.mutation = mutation
        self.thinking = thinking

    def chat(self, messages, tools=None, temperature=0.0):
        self.histories.append(copy.deepcopy(messages))
        self.tools_seen.append(copy.deepcopy(tools))
        self.temperatures.append(temperature)
        number = len(self.histories)
        common = {
            "usage": Usage(prompt_tokens=20, completion_tokens=5, source="reported", reported_total_tokens=25),
            "model": self.model, "request_id": f"request-{number}", "request_attempts": number,
            "reasoning_content": _PRIVATE_REASONING if self.thinking == "enabled" else None,
        }
        if number == 1:
            response = LLMResponse(content_is_null=True, finish_reason="tool_calls", tool_calls=[
                ToolCall(id="get-1", name="get_probe_token", arguments={}, raw_arguments="{  }")
            ], **common)
        elif number == 2:
            self.token = json.loads(messages[-1]["content"])["token"]
            arguments = {"token": self.token}
            response = LLMResponse(content_is_null=True, finish_reason="tool_calls", tool_calls=[
                ToolCall(id="verify-2", name="verify_probe_token", arguments=arguments,
                         raw_arguments=json.dumps(arguments))
            ], **common)
        elif number == 3:
            if json.loads(messages[-1]["content"]) != {"verified": True}:
                raise AssertionError("verification reply was not delivered")
            response = LLMResponse(text="PROBE_OK", finish_reason="stop", **common)
        else:
            raise AssertionError("more than three logical calls")
        return self.mutation(number, response) if self.mutation else response


class ProviderSmokeTest(unittest.TestCase):
    def test_offline_success_requires_two_real_tool_round_trips(self) -> None:
        provider = ProbeProvider()
        report = run_provider_smoke(provider)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["reason_code"], "ok")
        self.assertFalse(report["live_verified"])
        self.assertEqual(report["evidence_level"], "L1")
        self.assertEqual(report["logical_calls"], 3)
        self.assertEqual(len(provider.histories), 3)
        self.assertEqual(provider.temperatures, [0.0] * 3)
        self.assertEqual(provider.histories[1][-1]["tool_call_id"], "get-1")
        self.assertEqual(provider.histories[2][-1]["tool_call_id"], "verify-2")
        self.assertEqual(provider.histories[1][-2]["tool_calls"][0]["function"]["arguments"], "{  }")
        self.assertIsNone(provider.histories[1][-2]["content"])
        self.assertEqual([c["request_attempts"] for c in report["calls"]], [1, 2, 3])
        self.assertTrue(all(c["usage"]["source"] == "reported" for c in report["calls"]))
        self.assertEqual(report["calls"][2]["request_id"], "request-3")

    def test_token_is_fresh_and_absent_before_first_tool_reply(self) -> None:
        providers = [ProbeProvider(), ProbeProvider()]
        reports = [run_provider_smoke(provider) for provider in providers]
        self.assertNotEqual(providers[0].token, providers[1].token)
        for provider, report in zip(providers, reports):
            self.assertGreaterEqual(len(provider.token), 40)
            self.assertNotIn(provider.token, json.dumps(provider.histories[0]))
            self.assertNotIn(provider.token, json.dumps(report))

    def test_thinking_history_uses_serializer_without_public_reasoning(self) -> None:
        provider = ProbeProvider(thinking="enabled")
        report = run_provider_smoke(provider, temperature=0.25)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(provider.temperatures, [0.25] * 3)
        assistants = [m for m in provider.histories[-1] if m["role"] == "assistant"]
        self.assertEqual([m["reasoning_content"] for m in assistants], [_PRIVATE_REASONING] * 2)
        self.assertTrue(all(c["has_reasoning"] for c in report["calls"]))
        self.assertNotIn(_PRIVATE_REASONING, json.dumps(report))
        self.assertNotIn(_SECRET, json.dumps(report))

    def test_live_category_does_not_claim_a_failed_sequence_is_verified(self) -> None:
        # This tests classification only; the fixture itself is not live evidence.
        report = run_provider_smoke(ProbeProvider(), live=True)
        self.assertEqual(report["evidence_level"], "L2")
        self.assertTrue(report["live_verified"])
        broken = ProbeProvider(lambda n, response: response.model_copy(update={"text": "wrong"}) if n == 3 else response)
        report = run_provider_smoke(broken, live=True)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["evidence_level"], "L2")
        self.assertFalse(report["live_verified"])

    def test_early_final_answer_does_not_pass_without_tool_calls(self) -> None:
        provider = ProbeProvider(lambda _n, _r: LLMResponse(text="PROBE_OK", finish_reason="stop"))
        report = run_provider_smoke(provider)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["reason_code"], "unexpected_finish_reason")
        self.assertEqual(report["logical_calls"], 1)

    def test_unknown_tool_is_rejected_without_execution(self) -> None:
        def mutate(_n, response):
            return response.model_copy(update={"tool_calls": [ToolCall(id="x", name="run_python", arguments={"code": "raise RuntimeError"})]})
        report = run_provider_smoke(ProbeProvider(mutate))
        self.assertEqual(report["reason_code"], "unexpected_tool_name")
        self.assertEqual(report["logical_calls"], 1)
        self.assertNotIn("raise RuntimeError", json.dumps(report))

    def test_multiple_calls_in_one_response_are_rejected(self) -> None:
        provider = ProbeProvider(lambda _n, r: r.model_copy(update={"tool_calls": r.tool_calls * 2}))
        self.assertEqual(run_provider_smoke(provider)["reason_code"], "expected_single_tool_call")
        self.assertEqual(len(provider.histories), 1)

    def test_verify_must_use_exact_token_and_no_extra_arguments(self) -> None:
        for replacement, expected in (
            ({"token": "guessed"}, "probe_token_mismatch"),
            ({"token": "非 ASCII"}, "probe_token_mismatch"),
            ({"token": 7}, "invalid_tool_arguments"),
            ({"token": "wrong", "extra": True}, "invalid_tool_arguments"),
        ):
            with self.subTest(replacement=replacement):
                def mutate(n, response):
                    if n == 2:
                        call = response.tool_calls[0].model_copy(update={"arguments": replacement, "raw_arguments": json.dumps(replacement)})
                        return response.model_copy(update={"tool_calls": [call]})
                    return response
                provider = ProbeProvider(mutate)
                report = run_provider_smoke(provider)
                self.assertEqual(report["reason_code"], expected)
                self.assertEqual(report["logical_calls"], 2)

    def test_invalid_or_inconsistent_raw_json_is_not_silently_repaired(self) -> None:
        for raw, parsed, parse_error in (
            ("", {}, None), ("[]", {}, None), ('{"extra":1}', {}, None),
            ('{"x":1,"x":2}', {"x": 2}, None), ('{"x":NaN}', {}, None),
            ("{}", {}, "parse-error-sentinel"),
        ):
            with self.subTest(raw=raw):
                def mutate(_n, response):
                    call = response.tool_calls[0].model_copy(update={"raw_arguments": raw, "arguments": parsed, "parse_error": parse_error})
                    return response.model_copy(update={"tool_calls": [call]})
                report = run_provider_smoke(ProbeProvider(mutate))
                self.assertEqual(report["reason_code"], "invalid_tool_arguments")
                self.assertNotIn("parse-error-sentinel", json.dumps(report))

    def test_call_ids_cannot_be_empty(self) -> None:
        for bad_id in ("", " "):
            with self.subTest(bad_id=bad_id):
                def mutate(n, response):
                    if n == 2:
                        return response.model_copy(update={"tool_calls": [response.tool_calls[0].model_copy(update={"id": bad_id})]})
                    return response
                report = run_provider_smoke(ProbeProvider(mutate))
                self.assertEqual(report["reason_code"], "invalid_tool_call_id")

    def test_closed_batches_may_reuse_the_same_tool_call_id(self) -> None:
        def mutate(n, response):
            if n == 2:
                return response.model_copy(update={"tool_calls": [response.tool_calls[0].model_copy(update={"id": "get-1"})]})
            return response
        provider = ProbeProvider(mutate)
        report = run_provider_smoke(provider)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(provider.histories[1][-1]["tool_call_id"], "get-1")
        self.assertEqual(provider.histories[2][-1]["tool_call_id"], "get-1")

    def test_thinking_without_reasoning_field_fails(self) -> None:
        provider = ProbeProvider(lambda _n, r: r.model_copy(update={"reasoning_content": None}), thinking="enabled")
        report = run_provider_smoke(provider)
        self.assertEqual(report["reason_code"], "missing_reasoning")
        self.assertEqual(report["logical_calls"], 1)

    def test_empty_reasoning_string_is_present_and_valid(self) -> None:
        provider = ProbeProvider(lambda _n, r: r.model_copy(update={"reasoning_content": ""}), thinking="enabled")
        report = run_provider_smoke(provider)
        self.assertEqual(report["status"], "passed")
        self.assertTrue(all(call["has_reasoning"] for call in report["calls"]))

    def test_contradictory_null_content_is_rejected(self) -> None:
        provider = ProbeProvider(lambda _n, r: r.model_copy(update={"content_is_null": True, "text": "nonempty"}))
        report = run_provider_smoke(provider)
        self.assertEqual(report["reason_code"], "invalid_response")
        self.assertEqual(report["logical_calls"], 1)

    def test_final_turn_cannot_trigger_a_fourth_call(self) -> None:
        def mutate(n, response):
            if n == 3:
                return response.model_copy(update={"finish_reason": "tool_calls", "tool_calls": [ToolCall(id="again", name="get_probe_token")]})
            return response
        provider = ProbeProvider(mutate)
        report = run_provider_smoke(provider)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["logical_calls"], 3)
        self.assertEqual(len(provider.histories), 3)

    def test_provider_errors_expose_only_static_codes_and_attempt_counts(self) -> None:
        class Broken(ProbeProvider):
            def chat(self, *args, **kwargs):
                raise ProviderError(_SECRET, code=_PRIVATE_REASONING, attempts=3)
        report = run_provider_smoke(Broken(), live=True)
        self.assertEqual(report["reason_code"], "provider_error")
        self.assertEqual(report["calls"][0]["request_attempts"], 3)
        self.assertNotIn(_SECRET, json.dumps(report))
        self.assertNotIn(_PRIVATE_REASONING, json.dumps(report))
        self.assertFalse(report["live_verified"])

    def test_unexpected_exception_is_sanitized(self) -> None:
        class Broken(ProbeProvider):
            def chat(self, *args, **kwargs):
                raise RuntimeError(_SECRET)
        report = run_provider_smoke(Broken())
        self.assertEqual(report["reason_code"], "unexpected_provider_error")
        self.assertNotIn(_SECRET, json.dumps(report))

    def test_metadata_ids_cannot_echo_known_secrets(self) -> None:
        provider = ProbeProvider()
        def mutate(n, response):
            identity = _SECRET if n == 1 else provider.token
            return response.model_copy(update={"request_id": identity, "model": _PRIVATE_REASONING})
        provider.mutation = mutate
        report = run_provider_smoke(provider)
        self.assertEqual(report["status"], "passed")
        self.assertTrue(all(c["request_id"] is None for c in report["calls"]))
        self.assertNotIn(provider.token, json.dumps(report))
        self.assertNotIn(_SECRET, json.dumps(report))
        self.assertNotIn(_PRIVATE_REASONING, json.dumps(report))

        reasoning_id = ProbeProvider(
            lambda _n, r: r.model_copy(update={"request_id": _PRIVATE_REASONING}), thinking="enabled",
        )
        report = run_provider_smoke(reasoning_id)
        self.assertTrue(all(c["request_id"] is None for c in report["calls"]))
        self.assertNotIn(_PRIVATE_REASONING, json.dumps(report))

    def test_invalid_probe_configuration_makes_no_calls(self) -> None:
        for kwargs in ({"temperature": True}, {"temperature": float("nan")}, {"temperature": 3}, {"live": "yes"}):
            with self.subTest(kwargs=kwargs):
                provider = ProbeProvider()
                report = run_provider_smoke(provider, **kwargs)
                self.assertEqual(report["reason_code"], "invalid_probe_config")
                self.assertEqual(provider.histories, [])
                self.assertFalse(report["live_verified"])

    def test_actual_adapter_round_trip_with_fake_http_preserves_reasoning(self) -> None:
        provider = DeepSeekProvider(api_key="test-key", thinking="enabled")
        payloads = []

        def post(payload):
            payloads.append(copy.deepcopy(payload))
            number = len(payloads)
            message = {"role": "assistant", "content": None, "reasoning_content": _PRIVATE_REASONING}
            if number <= 2:
                arguments = {} if number == 1 else {"token": json.loads(payload["messages"][-1]["content"])["token"]}
                message["tool_calls"] = [{"id": f"call-{number}", "type": "function", "function": {
                    "name": "get_probe_token" if number == 1 else "verify_probe_token",
                    "arguments": json.dumps(arguments),
                }}]
                finish = "tool_calls"
            else:
                message["content"], finish = "PROBE_OK", "stop"
            return HTTPResult(data={"id": f"response-{number}", "model": "deepseek-flash",
                "choices": [{"message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}}, attempts=1)

        with patch.object(provider, "_post_with_retry", side_effect=post):
            report = run_provider_smoke(provider)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["evidence_level"], "L1")
        self.assertEqual(len(payloads), 3)
        self.assertTrue(all(payload["thinking"] == {"type": "enabled"} for payload in payloads))
        assistants = [m for m in payloads[-1]["messages"] if m["role"] == "assistant"]
        self.assertEqual([m["reasoning_content"] for m in assistants], [_PRIVATE_REASONING] * 2)
        self.assertNotIn(_PRIVATE_REASONING, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
