"""LLM Provider：把"调模型"收敛到一个接口，便于 Mock 与切换。

对应课程要求 2.6：只能用 DeepSeek 或 HKGAI，禁止 OpenAI/Anthropic。
=> 所以 provider 是**可替换的一层**，而不是散落在各处的 HTTP 调用。

两个实现：
  - ``DeepSeekProvider``：真实调用，OpenAI 兼容协议（用标准库 urllib，不装 SDK 也能跑）
  - ``ScriptedProvider``：按剧本返回固定响应。不需要 API key、不联网、100% 可复现。
    单元测试和 key 还没发下来时全靠它。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .schemas import LLMResponse, ToolCall, ToolSpec, Usage


class ProviderError(RuntimeError):
    """模型调用失败（网络、鉴权、限流、返回格式不对）。"""


class Provider(ABC):
    name: str = "provider"

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """发一次对话请求，返回归一化后的 LLMResponse。"""


# ---------------------------------------------------------------------------
# 真实 provider
# ---------------------------------------------------------------------------
class DeepSeekProvider(Provider):
    """DeepSeek Chat Completions（OpenAI 兼容）。

    生产环境你可以直接换成 ``openai`` SDK，一行等价：

        client = OpenAI(api_key=..., base_url="https://api.deepseek.com")
        resp = client.chat.completions.create(model=..., messages=..., tools=...)

    这里用 urllib 写，是为了让你看清线上到底发了什么、收到了什么 ——
    调试 agent 时，"模型实际看到了什么"永远是第一现场。
    """

    name = "deepseek"
    RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-flash",
        timeout: float = 120.0,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise ProviderError(
                "缺少 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填入密钥，"
                "或先用 `python -m miniagent demo` 走离线脚本模式。"
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        # temperature=0 时故意不传：思考模式下该参数无效，传了反而容易误解。
        if temperature:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = [spec.to_api() for spec in tools]
            payload["tool_choice"] = "auto"

        started = time.perf_counter()
        data = self._post_with_retry(payload)
        latency_ms = (time.perf_counter() - started) * 1000
        return self._parse(data, latency_ms)

    # ------------------------------------------------------------------ HTTP
    def _post_with_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return self._post(payload)
            except _Retryable as exc:
                last_error = exc
                # 指数退避：0.5s, 1s, 2s。真实项目还必须加上抖动，避免所有人同时重试。
                time.sleep(0.5 * (2**attempt))
        raise ProviderError(f"调用 DeepSeek 失败（已重试 {self.max_retries} 次）：{last_error}")

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            if exc.code in self.RETRYABLE_STATUS:
                raise _Retryable(f"HTTP {exc.code}: {detail}") from exc
            raise ProviderError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise _Retryable(f"网络错误: {exc.reason}") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"响应不是合法 JSON: {raw[:400]}") from exc

    # ----------------------------------------------------------------- 解析
    def _parse(self, data: dict[str, Any], latency_ms: float) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"响应里没有 choices: {json.dumps(data, ensure_ascii=False)[:400]}")

        choice = choices[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}

        tool_calls: list[ToolCall] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            raw_arguments = function.get("arguments")
            if isinstance(raw_arguments, dict):  # 少数实现直接给对象
                arguments, raw_text, parse_error = raw_arguments, json.dumps(
                    raw_arguments, ensure_ascii=False
                ), None
            else:
                raw_text = "" if raw_arguments is None else str(raw_arguments)
                try:
                    arguments = json.loads(raw_text) if raw_text.strip() else {}
                    parse_error = None if isinstance(arguments, dict) else "参数不是 JSON 对象"
                    if not isinstance(arguments, dict):
                        arguments = {}
                except json.JSONDecodeError as exc:
                    arguments, parse_error = {}, f"参数不是合法 JSON: {exc}"

            tool_calls.append(
                ToolCall(
                    id=str(call.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                    raw_arguments=raw_text,
                    parse_error=parse_error,
                )
            )

        return LLMResponse(
            text=message.get("content") or "",
            tool_calls=tool_calls,
            usage=Usage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
            ),
            finish_reason=str(choice.get("finish_reason") or ""),
            latency_ms=round(latency_ms, 2),
            model=str(data.get("model") or self.model),
        )


class _Retryable(Exception):
    """内部标记：这个错误值得重试（限流/5xx/网络抖动）。"""


# ---------------------------------------------------------------------------
# 离线 provider
# ---------------------------------------------------------------------------
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
        usage = Usage(prompt_tokens=prompt_tokens, completion_tokens=32)

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
