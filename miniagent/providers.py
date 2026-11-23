"""DeepSeek Chat Completions and deterministic offline providers.

The transport retries only HTTP. Tools run later in RAO, so a retried model
request cannot replay local tool side effects. Private reasoning stays in the
protocol history and is never included in public error messages.
"""
from __future__ import annotations

import json
import math
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import deepseek_markup
from .config import (DEFAULT_MODEL, DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_REQUEST_TIMEOUT_SECONDS,
                     DEFAULT_REQUEST_DEADLINE_SECONDS, validate_provider_options)
from .provider_http import HTTPResult, ProviderError, post_json
from .schemas import LLMResponse, ToolCall, ToolSpec, Usage


class Provider(ABC):
    name: str = "provider"
    transport: str = "unknown"
    supports_streaming = False

    @abstractmethod
    def chat(self, messages: list[dict[str, Any]], tools: list[ToolSpec] | None = None,
             temperature: float = 0.0) -> LLMResponse:
        """Return one response; never execute tools in this method."""


def provider_configuration(provider: Provider) -> dict[str, Any]:
    """Public execution knobs only; never serialize a provider's attributes.

    A trace needs the effective output ceiling to explain truncation. Credentials,
    endpoint URLs, prompts and private reasoning are deliberately excluded.
    """
    result = {"provider": provider.name, "transport": provider.transport}
    if isinstance(provider, DeepSeekProvider):
        result.update(
            model=provider.model, thinking=provider.thinking,
            official_api=urlsplit(provider.base_url).hostname == "api.deepseek.com",
            max_output_tokens=provider.max_output_tokens,
            request_timeout_seconds=provider.timeout,
            request_deadline_seconds=provider.deadline_seconds,
            max_retries=provider.max_retries,
        )
    return result


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProviderError(message, code="invalid_response")


def _strict_json_object(text: str) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("non-finite JSON number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number

    result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant,
                        parse_float=finite_float)
    if not isinstance(result, dict):
        raise ValueError("arguments must be a JSON object")
    return result


def _collect_deepseek_stream(events: Iterator[dict], notify: Callable[[str, str], None],
                            should_cancel: Callable[[], bool] | None, *, secret: str = "") -> dict:
    """Rebuild the ordinary completion envelope. Only assistant content reaches notify."""
    identity = {}
    content, reasoning = [], []
    calls = {}
    pending = ""
    markup_pending = ""
    in_markup = False
    thinking_announced = False
    finish = usage = None
    try:
        for event in events:
            if should_cancel and should_cancel():
                raise ProviderError("Provider request cancelled", code="cancelled")
            _require("error" not in event, "Provider stream returned an error")
            for key in ("model", "id"):
                if key in event:
                    _require(isinstance(event[key], str) and identity.get(key, event[key]) == event[key],
                             "Conflicting provider stream identity")
                    identity[key] = event[key]
            if event.get("usage") is not None:
                _require(usage is None, "Duplicate stream usage")
                usage = event["usage"]
            choices = event.get("choices")
            if choices == [] and event.get("usage") is not None:
                continue  # Some OpenAI-compatible endpoints send a separate usage event.
            _require(isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict),
                     "Invalid provider stream choices")
            choice = choices[0]
            _require(choice.get("index", 0) == 0 and finish is None, "Unexpected stream choice")
            delta = choice.get("delta")
            _require(isinstance(delta, dict) and delta.get("role", "assistant") in ("assistant", None),
                     "Invalid assistant stream delta")
            fragments = delta.get("tool_calls")
            if fragments is None:
                fragments = []
            _require(isinstance(fragments, list), "Invalid stream tool calls")
            for item in fragments:
                _require(isinstance(item, dict), "Invalid stream tool fragment")
                index = item.get("index")
                _require(type(index) is int and index >= 0, "Invalid stream tool index")
                call = calls.setdefault(index, {"id": None, "type": None, "names": [], "arguments": []})
                for key in ("id", "type"):
                    value = item.get(key)
                    if value is not None:
                        _require(isinstance(value, str) and (call[key] is None or call[key] == value),
                                 "Conflicting stream tool identity")
                        call[key] = value
                function = item.get("function", {})
                _require(isinstance(function, dict), "Invalid stream function fragment")
                for source, target in (("name", "names"), ("arguments", "arguments")):
                    part = function.get(source)
                    if part is not None:
                        _require(isinstance(part, str), "Invalid stream function text")
                        call[target].append(part)
            for field, parts in (("reasoning_content", reasoning), ("content", content)):
                part = delta.get(field)
                if part is not None:
                    _require(isinstance(part, str), "Invalid assistant stream text")
                    parts.append(part)
                    if field == "reasoning_content" and part and not thinking_announced:
                        thinking_announced = True
                        notify("thinking", "")
                    if field == "content" and part and not in_markup:
                        visible, markup_pending, in_markup = deepseek_markup.public_prefix(markup_pending + part)
                        visible = pending + visible
                        pending = ""
                        if secret:
                            visible = visible.replace(secret, "[redacted]")
                            # Hold a possible key prefix across provider chunks.
                            for n in range(min(len(visible), len(secret)-1), 0, -1):
                                if visible.endswith(secret[:n]):
                                    visible, pending = visible[:-n], visible[-n:]
                                    break
                        if visible:
                            notify("delta", visible)
            finish = choice.get("finish_reason")
        _require(isinstance(finish, str), "Provider stream lacks a final choice")
        _require(sorted(calls) == list(range(len(calls))), "Incomplete stream tool indexes")
        message = {"role": "assistant", "content": "".join(content) if content else None}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            message["tool_calls"] = [{"id": call["id"], "type": call["type"],
                                      "function": {"name": "".join(call["names"]), "arguments": "".join(call["arguments"])}}
                                     for _, call in sorted(calls.items())]
        if pending or markup_pending:
            notify("delta", pending + markup_pending)
        return {**identity, "usage": usage, "choices": [{"finish_reason": finish, "message": message}]}
    except Exception:
        # The next HTTP attempt starts with empty accumulators and a new preview.
        notify("discard", "")
        raise


class DeepSeekProvider(Provider):
    """Official API adapter with opt-in streaming and explicit thinking configuration.

    max_retries means additional HTTP attempts, not total attempts. The request
    deadline is separate from the future root BudgetLedger. Environment values
    are read by Settings/CLI, never implicitly by this constructor.
    """
    supports_streaming = True
    name = "deepseek"
    transport = "http"
    FINISH_REASONS = {"stop", "tool_calls", "length", "content_filter",
                      "insufficient_system_resource", "aborted"}

    def __init__(
        self, api_key: str, base_url: str = "https://api.deepseek.com",
        model: str = DEFAULT_MODEL, timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS, max_retries: int = 2,
        *, thinking: str = "disabled", reasoning_effort: str = "high",
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        deadline_seconds: float = DEFAULT_REQUEST_DEADLINE_SECONDS,
    ) -> None:
        if type(api_key) is not str or not api_key.strip():
            raise ProviderError(
                "缺少 DEEPSEEK_API_KEY。请设置本机环境变量；.env 不会自动加载。"
                "也可运行 `python -m miniagent demo` 使用离线模式。",
                code="missing_api_key",
            )
        try:
            validate_provider_options(
                model=model, base_url=base_url, thinking=thinking,
                reasoning_effort=reasoning_effort, max_output_tokens=max_output_tokens,
                request_timeout_seconds=timeout, request_deadline_seconds=deadline_seconds,
                max_retries=max_retries,
            )
        except ValueError as exc:
            raise ProviderError(str(exc), code="invalid_config") from None
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model, self.thinking = model, thinking
        self.reasoning_effort, self.max_output_tokens = reasoning_effort, max_output_tokens
        self.timeout, self.deadline_seconds, self.max_retries = timeout, deadline_seconds, max_retries
        # Studio creates a provider per run and shares it across its members.
        # Keep only safe state, not the original exception or its traceback.
        self._insufficient_balance = False

    @staticmethod
    def _balance_error(attempts: int) -> ProviderError:
        return ProviderError(
            "DeepSeek 账户额度不足（HTTP 402）。重试需要可用额度，可在满足条件后重新发起任务；"
            "本轮不会继续发送模型请求，已保存的工作仍然保留。",
            code="insufficient_balance", status_code=402, attempts=attempts, retryable=False,
        )

    def _build_payload(self, messages: list[dict[str, Any]],
                       tools: list[ToolSpec] | None, temperature: float) -> dict[str, Any]:
        try:
            validate_provider_options(
                model=self.model, base_url=self.base_url, thinking=self.thinking,
                reasoning_effort=self.reasoning_effort, max_output_tokens=self.max_output_tokens,
                request_timeout_seconds=self.timeout, request_deadline_seconds=self.deadline_seconds,
                max_retries=self.max_retries, temperature=temperature,
            )
            # Own a complete snapshot before HTTP retry starts. No mutation of
            # the caller's history, and no rereading changing objects on retry.
            history = json.loads(json.dumps(messages, ensure_ascii=False, allow_nan=False))
            self._validate_history(history, require_reasoning=bool(tools) and self.thinking == "enabled")
            payload = {"model": self.model, "messages": history, "stream": False,
                       "thinking": {"type": self.thinking}, "max_tokens": self.max_output_tokens}
            if self.thinking == "enabled":
                payload["reasoning_effort"] = self.reasoning_effort
            else:
                payload["temperature"] = temperature  # zero is an explicit choice
            if tools:
                payload["tools"] = [tool.to_api() for tool in tools]
                payload["tool_choice"] = "auto"
            return payload
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise ProviderError("Invalid DeepSeek request configuration or message history", code="invalid_request") from None

    @staticmethod
    def _validate_history(history: Any, *, require_reasoning: bool) -> None:
        if not isinstance(history, list) or not history:
            raise ValueError("messages must be nonempty")
        pending: set[str] = set()
        for message in history:
            if not isinstance(message, dict):
                raise ValueError("invalid message")
            role = message.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                raise ValueError("unsupported role")
            content = message.get("content")
            if not isinstance(content, str) and not (role == "assistant" and content is None):
                raise ValueError("text-only adapter requires text content")
            if role == "tool":
                identity = message.get("tool_call_id")
                if not isinstance(identity, str) or identity not in pending:
                    raise ValueError("orphan or duplicate tool result")
                pending.remove(identity)
                continue
            if pending:
                raise ValueError("tool batch has unanswered calls")
            if role == "assistant":
                if require_reasoning and not isinstance(message.get("reasoning_content"), str):
                    raise ValueError("thinking tool history requires reasoning_content")
                if "reasoning_content" in message and message["reasoning_content"] is not None and not isinstance(message["reasoning_content"], str):
                    raise ValueError("invalid reasoning field")
                calls = message.get("tool_calls", [])
                if not isinstance(calls, list):
                    raise ValueError("tool_calls must be an array")
                for call in calls:
                    if not isinstance(call, dict) or call.get("type") != "function":
                        raise ValueError("unsupported tool call")
                    identity = call.get("id")
                    function = call.get("function")
                    if (not isinstance(identity, str) or not identity.strip() or identity in pending
                            or not isinstance(function, dict) or not isinstance(function.get("name"), str)
                            or not function["name"].strip() or not isinstance(function.get("arguments"), str)):
                        raise ValueError("invalid tool call")
                    pending.add(identity)
        if pending:
            raise ValueError("tool batch has unanswered calls")

    def chat(self, messages: list[dict[str, Any]], tools: list[ToolSpec] | None = None,
             temperature: float = 0.0, *, max_output_tokens: int | None = None,
             deadline_seconds: float | None = None, required_tool: str | None = None,
             on_stream: Callable[[str, str], None] | None = None,
             should_cancel: Callable[[], bool] | None = None,
             tool_choice: str | None = None) -> LLMResponse:
        if self._insufficient_balance:
            raise self._balance_error(0)
        payload = self._build_payload(messages, tools, temperature)
        if tool_choice is not None:
            if tool_choice not in {"auto", "none"} or not tools:
                raise ProviderError("tool_choice must be auto or none with declared tools", code="invalid_config")
            payload["tool_choice"] = tool_choice
        if required_tool is not None:
            if self.thinking != "disabled" or required_tool not in {tool.name for tool in tools or []}:
                raise ProviderError("Named tool choice requires a declared tool and thinking disabled", code="invalid_config")
            payload["tool_choice"] = {"type": "function", "function": {"name": required_tool}}
        if max_output_tokens is not None:
            if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= self.max_output_tokens:
                raise ProviderError("Invalid per-request output allocation", code="invalid_config")
            payload["max_tokens"] = max_output_tokens
        started = time.perf_counter()
        options = {"deadline_seconds": deadline_seconds} if deadline_seconds is not None else {}
        if on_stream is not None:
            payload.update(stream=True, stream_options={"include_usage": True})
            if should_cancel is not None:
                options["should_cancel"] = should_cancel
            options["stream_decoder"] = lambda events: _collect_deepseek_stream(events, on_stream, should_cancel, secret=self.api_key)
        attempts = 0
        prior_usage = Usage()
        for attempt in range(2):
            try:
                result = self._post_with_retry(payload, **options)
            except ProviderError as exc:
                exc.attempts += attempts
                if exc.status_code == 402 and urlsplit(self.base_url).hostname == "api.deepseek.com":
                    self._insufficient_balance = True
                    raise self._balance_error(exc.attempts) from None
                raise
            attempts += result.attempts
            try:
                response = self._parse(result.data, (time.perf_counter() - started) * 1000)
                if response.tool_calls and (tool_choice == "none" or (required_tool and
                        any(call.name != required_tool for call in response.tool_calls))):
                    raise ProviderError("Provider returned a disabled tool", code="invalid_response")
                if self.thinking == "enabled" and tools and response.reasoning_content is None:
                    raise ProviderError("Thinking tool response is missing reasoning_content", code="invalid_response")
            except ProviderError as exc:
                exc.attempts = attempts
                if exc.code != "invalid_tool_markup" or attempt:
                    raise
                if on_stream:
                    on_stream("discard", "")
                if should_cancel and should_cancel():
                    raise ProviderError("Provider request cancelled", code="cancelled", attempts=attempts)
                remaining = min(self.deadline_seconds, deadline_seconds or self.deadline_seconds) - (time.perf_counter()-started)
                if remaining <= 0:
                    raise
                options["deadline_seconds"] = remaining
                prior_usage = self._parse_usage(result.data.get("usage"))
                note = "Use the API's structured tool_calls field for invocations. Return ordinary text for a conversational answer."
                history = payload["messages"]
                if history[0]["role"] == "system":
                    history[0] = {**history[0], "content":history[0]["content"] + "\n" + note}
                else:
                    history.insert(0, {"role":"system", "content":note})
                continue
            return response.model_copy(update={"request_attempts":attempts, "usage":prior_usage.merged(response.usage),
                                               "text":response.text.replace(self.api_key, "[redacted]")})

    def _post_with_retry(self, payload: dict[str, Any], *, deadline_seconds: float | None = None,
                         stream_decoder: Callable[[Iterator[dict]], dict] | None = None,
                         should_cancel: Callable[[], bool] | None = None) -> HTTPResult:
        return post_json(
            f"{self.base_url}/chat/completions", payload, api_key=self.api_key,
            timeout=self.timeout, deadline_seconds=(self.deadline_seconds if deadline_seconds is None else
                                                   min(self.deadline_seconds, deadline_seconds)),
            max_retries=self.max_retries, **({"stream_decoder": stream_decoder} if stream_decoder else {}),
            **({"should_cancel":should_cancel} if should_cancel else {}),
        )

    def _parse(self, data: dict[str, Any], latency_ms: float) -> LLMResponse:
        _require(isinstance(data, dict), "DeepSeek response must be an object")
        choices = data.get("choices")
        _require(isinstance(choices, list) and len(choices) == 1, "DeepSeek response requires exactly one choice")
        choice = choices[0]
        _require(isinstance(choice, dict), "Invalid DeepSeek choice")
        message = choice.get("message")
        _require(isinstance(message, dict) and message.get("role") == "assistant", "Invalid assistant response envelope")
        _require("content" in message, "Missing assistant content field")
        content = message.get("content")
        reasoning = message.get("reasoning_content")
        _require(content is None or isinstance(content, str), "Invalid assistant content type")
        _require(reasoning is None or isinstance(reasoning, str), "Invalid reasoning_content type")
        finish = choice.get("finish_reason")
        _require(isinstance(finish, str) and finish in self.FINISH_REASONS, "Invalid response finish_reason")
        calls = message.get("tool_calls", [])
        _require(isinstance(calls, list), "Invalid tool_calls array")
        content = deepseek_markup.public_content(content, calls)
        _require(finish != "tool_calls" or bool(calls), "tool_calls finish without calls")
        _require(finish != "stop" or not calls, "stop finish conflicts with tool calls")
        parsed, identities = [], set()
        for call in calls:
            _require(isinstance(call, dict) and call.get("type") == "function", "Unsupported tool call type")
            identity = call.get("id")
            function = call.get("function")
            _require(isinstance(identity, str) and bool(identity.strip()) and identity not in identities,
                     "Missing or duplicate tool call id")
            identities.add(identity)
            _require(isinstance(function, dict), "Invalid tool function envelope")
            name, raw = function.get("name"), function.get("arguments")
            _require(isinstance(name, str) and bool(name.strip()), "Invalid tool function name")
            _require(isinstance(raw, str), "Tool arguments must be an original JSON string")
            try:
                arguments, parse_error = _strict_json_object(raw), None
            except (ValueError, RecursionError):
                arguments, parse_error = {}, "参数必须是无重复键、有限数值的有效 JSON 对象"
            parsed.append(ToolCall(id=identity, type="function", name=name,
                                   arguments=arguments, raw_arguments=raw, parse_error=parse_error))
        usage = self._parse_usage(data.get("usage"))
        request_id, model = data.get("id"), data.get("model", self.model)
        _require(request_id is None or isinstance(request_id, str), "Invalid request id")
        _require(isinstance(model, str) and bool(model.strip()), "Invalid response model")
        return LLMResponse(
            text=content or "", content_is_null=content is None, reasoning_content=reasoning,
            tool_calls=parsed, usage=usage, finish_reason=finish,
            latency_ms=round(latency_ms, 2), model=model, request_id=request_id,
        )

    @staticmethod
    def _parse_usage(raw: Any) -> Usage:
        if raw is None:
            return Usage(source="unknown")
        _require(isinstance(raw, dict), "Invalid usage object")
        def count(value, required=False):
            if value is None and not required:
                return None
            _require(type(value) is int and value >= 0, "Usage counts must be nonnegative integers")
            return value
        prompt = count(raw.get("prompt_tokens"), required=True)
        completion = count(raw.get("completion_tokens"), required=True)
        total = count(raw.get("total_tokens"))
        _require(total is None or total == prompt + completion, "Inconsistent total usage")
        hit, miss = count(raw.get("prompt_cache_hit_tokens")), count(raw.get("prompt_cache_miss_tokens"))
        _require(hit is None or hit <= prompt, "Invalid prompt cache usage")
        _require(miss is None or miss <= prompt, "Invalid prompt cache usage")
        _require(hit is None or miss is None or hit + miss == prompt, "Inconsistent prompt cache usage")
        details = raw.get("completion_tokens_details")
        _require(details is None or isinstance(details, dict), "Invalid completion usage details")
        reasoning = count(details.get("reasoning_tokens")) if details else None
        _require(reasoning is None or reasoning <= completion, "Invalid reasoning usage")
        return Usage(prompt_tokens=prompt, completion_tokens=completion, source="reported",
                     reported_total_tokens=total, prompt_cache_hit_tokens=hit,
                     prompt_cache_miss_tokens=miss, reasoning_tokens=reasoning)


class ScriptedProvider(Provider):
    """按剧本返回固定响应 —— 离线、免费、完全确定性。

    剧本是一个 JSON 数组，每个元素代表模型的一次"回复"：

        {"text": "..."}                                        # 最终答复，任务结束
        {"tool_calls": [{"name": "write_file",
                         "arguments": {"path": "a.md", "content": "..."}}]}
        {"error": "模拟一次 API 失败"}                          # 抛 ProviderError

    为什么需要它：单元测试、CI、以及**密钥还没发下来时**先把整条链路跑通。
    这是这个项目能按时交付的关键工程习惯。
    """

    name = "scripted"
    transport = "offline"

    def __init__(self, script: list[dict[str, Any]], model: str = "scripted") -> None:
        self.script = list(script)
        self.model = model
        self.cursor = 0

    @classmethod
    def from_json(cls, path: Path | str) -> "ScriptedProvider":
        # utf-8-sig：Windows 记事本 / PowerShell 5.1 默认会给 UTF-8 加 BOM，
        # 用 utf-8 读会直接抛 "Unexpected UTF-8 BOM"。这种坑不该让使用者踩。
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(data, list):
            raise ValueError("剧本文件必须是一个 JSON 数组")
        return cls(data)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        **_options: Any,
    ) -> LLMResponse:
        if self.cursor >= len(self.script):
            # 剧本耗尽 = 这条任务的行为超出了预期，必须显式报错。
            # 如果这里假装"任务完成"，完成率就会被虚高 —— 那是最糟的一类 bug：
            # 指标骗了你，而你还拿它去写报告。
            raise ProviderError(
                f"剧本已耗尽（共 {len(self.script)} 条），任务在第 {self.cursor + 1} 次调用后仍未收尾。"
            )

        item = self.script[self.cursor]
        self.cursor += 1

        if "error" in item:
            raise ProviderError(str(item["error"]))

        prompt_tokens = sum(len(str(m.get("content") or "")) for m in messages) // 2 + 16
        usage = Usage(prompt_tokens=prompt_tokens, completion_tokens=32, source="estimated")

        if item.get("tool_calls"):
            calls = [
                ToolCall(
                    id=f"call_{self.cursor}_{index}",
                    name=str(tc.get("name") or ""),
                    arguments=dict(tc.get("arguments") or {}),
                    raw_arguments=json.dumps(tc.get("arguments") or {}, ensure_ascii=False),
                )
                for index, tc in enumerate(item["tool_calls"])
            ]
            return LLMResponse(
                text=str(item.get("text") or ""),
                tool_calls=calls,
                usage=usage,
                finish_reason="tool_calls",
                model=self.model,
            )

        return LLMResponse(
            text=str(item.get("text") or ""),
            usage=usage,
            finish_reason="stop",
            model=self.model,
        )
