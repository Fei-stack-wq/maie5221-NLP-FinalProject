"""A three-call, in-memory tool protocol probe with metadata-only reporting.

This does not invoke the agent's tool registry, write files, or run shell/code.
HTTP retry/deadline bounds remain the provider's responsibility. ``live=True``
is the trusted caller's assertion that it supplied a real API provider; the
default records only L1 fixture evidence, even when the sequence passes.
"""
from __future__ import annotations

import json
import re
import secrets
from typing import Any

from .config import SUPPORTED_MODELS, validate_provider_options
from .providers import Provider, ProviderError
from .schemas import LLMResponse, ToolCall, ToolSpec


_SYSTEM = (
    "Run this tool protocol probe exactly in order. First call get_probe_token "
    "with an empty JSON object. After receiving its tool result, call "
    "verify_probe_token with exactly the token from that result. Make exactly "
    "one tool call in each of those turns. After verification returns true, "
    "answer exactly PROBE_OK without further tool calls or explanation. "
    "Never invent the token."
)
_FINISH = {"stop", "tool_calls", "length", "content_filter", "insufficient_system_resource", "aborted"}
_ERRORS = {
    "provider_error", "missing_api_key", "invalid_config", "invalid_request", "invalid_response",
    "deadline_exceeded", "response_too_large", "redirect_refused", "http_error",
    "invalid_retry_after", "timeout", "network_error", "retry_after_exceeded",
}
_PROBE_ERRORS = {
    "invalid_tool_arguments", "unexpected_finish_reason", "expected_single_tool_call",
    "unexpected_tool_name", "invalid_tool_call_id", "invalid_response", "missing_reasoning",
    "invalid_final_answer", "probe_token_mismatch",
}
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,200}\Z")


class _ProbeFailure(Exception):
    """Only static codes authored in this module may reach the report."""


def _tools() -> list[ToolSpec]:
    return [
        ToolSpec(name="get_probe_token", description="Get a fresh unpredictable token for this in-memory probe.",
                 parameters={"type": "object", "properties": {}, "additionalProperties": False}),
        ToolSpec(name="verify_probe_token", description="Verify the exact token returned by get_probe_token.",
                 parameters={"type": "object", "properties": {"token": {"type": "string"}},
                             "required": ["token"], "additionalProperties": False}),
    ]


def _count(value: Any, minimum: int = 0) -> int | None:
    return value if type(value) is int and value >= minimum else None


def _model(value: Any) -> str | None:
    return value if type(value) is str and value in SUPPORTED_MODELS | {"scripted"} else None


def _call_metadata(response: LLMResponse, number: int) -> dict[str, Any]:
    usage = response.usage
    identity = response.request_id
    return {
        "call": number,
        "model": _model(response.model),
        "finish_reason": response.finish_reason if response.finish_reason in _FINISH else "unknown",
        "request_attempts": _count(response.request_attempts, 1),
        "request_id": identity if type(identity) is str and _REQUEST_ID.fullmatch(identity) else None,
        "has_reasoning": response.reasoning_content is not None,
        "usage": {
            "prompt_tokens": _count(usage.prompt_tokens),
            "completion_tokens": _count(usage.completion_tokens),
            "source": usage.source if usage.source in {"reported", "estimated", "unknown"} else "unknown",
            "reported_total_tokens": _count(usage.reported_total_tokens),
            "prompt_cache_hit_tokens": _count(usage.prompt_cache_hit_tokens),
            "prompt_cache_miss_tokens": _count(usage.prompt_cache_miss_tokens),
            "reasoning_tokens": _count(usage.reasoning_tokens),
        },
    }


def _arguments(call: ToolCall) -> dict[str, Any]:
    if call.parse_error is not None or type(call.arguments) is not dict:
        raise _ProbeFailure("invalid_tool_arguments")
    if call.raw_arguments is None:
        return call.arguments

    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def finite(_value):
        raise ValueError

    try:
        raw = json.loads(call.raw_arguments, object_pairs_hook=unique, parse_constant=finite)
    except (TypeError, ValueError, RecursionError):
        raise _ProbeFailure("invalid_tool_arguments") from None
    if type(raw) is not dict or raw != call.arguments:
        raise _ProbeFailure("invalid_tool_arguments")
    return raw


def _expected_call(response: LLMResponse, name: str) -> tuple[ToolCall, dict[str, Any]]:
    if response.finish_reason != "tool_calls":
        raise _ProbeFailure("unexpected_finish_reason")
    if len(response.tool_calls) != 1:
        raise _ProbeFailure("expected_single_tool_call")
    call = response.tool_calls[0]
    if call.type != "function" or call.name != name:
        raise _ProbeFailure("unexpected_tool_name")
    if type(call.id) is not str or not call.id.strip():
        raise _ProbeFailure("invalid_tool_call_id")
    return call, _arguments(call)


def run_provider_smoke(provider: Provider, *, live: bool = False, temperature: float = 0.0) -> dict:
    """Check get-token -> verify-token -> PROBE_OK in at most three chat calls.

    Return static failure codes and allowlisted metadata only. No caller/provider
    exception text, prompt, raw response, private reasoning or probe token is
    included in the result. L2 names a live probe's evidence category, including
    failed attempts; only a passed live sequence sets live_verified to true.
    """
    thinking = getattr(provider, "thinking", None)
    report = {
        "status": "failed", "live_verified": False, "evidence_level": "L2" if live is True else "L1",
        "reason_code": "invalid_probe_config", "model": _model(getattr(provider, "model", None)),
        "thinking": thinking if thinking in ("enabled", "disabled") else None,
        "logical_calls": 0, "calls": [],
    }
    token = ""
    key = getattr(provider, "api_key", "")
    private_reasoning: list[str] = []

    def finish(code: str) -> dict:
        report["reason_code"] = code
        if code == "ok":
            report.update(status="passed", live_verified=live, evidence_level="L2" if live else "L1")
        # IDs are API metadata, but must not accidentally echo known secrets.
        sensitive = [value for value in (token, key, *private_reasoning) if type(value) is str and value]
        for item in report["calls"]:
            identity = item.get("request_id")
            if identity and any(value in identity for value in sensitive):
                item["request_id"] = None
        return report

    try:
        if type(live) is not bool:
            return finish("invalid_probe_config")
        validate_provider_options(temperature=temperature)
    except ValueError:
        return finish("invalid_probe_config")
    history = [{"role": "system", "content": _SYSTEM},
               {"role": "user", "content": "Start the in-memory protocol probe now."}]
    tools = _tools()
    for number in range(1, 4):
        report["logical_calls"] = number
        try:
            response = provider.chat(history, tools, temperature=temperature)
            if not isinstance(response, LLMResponse):
                raise _ProbeFailure("invalid_response")
            if response.content_is_null and response.text:
                raise _ProbeFailure("invalid_response")
            if type(response.reasoning_content) is str and response.reasoning_content:
                private_reasoning.append(response.reasoning_content)
            report["calls"].append(_call_metadata(response, number))
            if thinking == "enabled" and response.reasoning_content is None:
                raise _ProbeFailure("missing_reasoning")
            if number == 3:
                if response.finish_reason != "stop":
                    raise _ProbeFailure("unexpected_finish_reason")
                if response.tool_calls or response.content_is_null or response.text.strip() != "PROBE_OK":
                    raise _ProbeFailure("invalid_final_answer")
                return finish("ok")
            name = "get_probe_token" if number == 1 else "verify_probe_token"
            call, arguments = _expected_call(response, name)
            if number == 1:
                if arguments:
                    raise _ProbeFailure("invalid_tool_arguments")
                token = secrets.token_urlsafe(32)
                result = {"token": token}
            else:
                if set(arguments) != {"token"} or type(arguments["token"]) is not str:
                    raise _ProbeFailure("invalid_tool_arguments")
                if not arguments["token"].isascii() or not secrets.compare_digest(arguments["token"], token):
                    raise _ProbeFailure("probe_token_mismatch")
                result = {"verified": True}
            history.append(response.to_assistant_message())
            history.append({"role": "tool", "tool_call_id": call.id,
                            "content": json.dumps(result, ensure_ascii=False)})
        except ProviderError as exc:
            report["calls"].append({"call": number, "request_attempts": _count(exc.attempts),
                                    "request_id": None})
            code = exc.code if type(exc.code) is str and exc.code in _ERRORS else "provider_error"
            return finish(code)
        except _ProbeFailure as exc:
            return finish(str(exc) if str(exc) in _PROBE_ERRORS else "invalid_response")
        except Exception:
            return finish("unexpected_provider_error")
    return finish("invalid_final_answer")  # defensive; the third turn always terminates


__all__ = ["run_provider_smoke"]
