"""系统的数据契约（pydantic 模型）。

这些模型就是"边界"：LLM 的输出在这里被校验，工具的输入输出在这里被定型，
trace 在这里被序列化。改这里的字段要谨慎 —— 它决定了整个系统的形状。

对应课程要求：
- ToolSpec / ToolCall  -> 2.3 行动空间（工具调用协议）
- Usage                -> 成本与 token 追踪
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StrictBool, StrictInt, StrictStr


class ToolSpec(BaseModel):
    """给模型看的工具说明书（JSON Schema）。"""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)

    def to_api(self) -> dict[str, Any]:
        """转成 OpenAI 兼容的 tools 数组元素。DeepSeek 用的是同一套协议。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
                or {"type": "object", "properties": {}},
            },
        }


class ToolCall(BaseModel):
    """模型"想调用某个工具"的一次请求。"""

    id: StrictStr
    type: Literal["function"] = "function"
    name: StrictStr
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: StrictStr | None = None
    """原始协议字符串；None 仅用于宿主构造调用，空串不能改写成 {}。"""

    parse_error: str | None = None
    """模型偶尔会返回非法 JSON —— 这不是异常，是一个必须优雅处理的事件。"""


class ToolResult(BaseModel):
    """工具执行结果。永远不要抛异常给 agent 循环，错误也是一种"结果"。"""

    ok: bool
    content: str
    meta: dict[str, Any] = Field(default_factory=dict)
    handed_off: bool = False  # The tool committed an asynchronous handoff with automatic delivery.

    @classmethod
    def success(cls, content: str, **meta: Any) -> "ToolResult":
        return cls(ok=True, content=content, meta=meta)

    @classmethod
    def failure(cls, content: str, **meta: Any) -> "ToolResult":
        return cls(ok=False, content=content, meta=meta)


TokenCount = Annotated[StrictInt, Field(ge=0)]
RequestAttempts = Annotated[StrictInt, Field(ge=1)]
UsageSource = Literal["reported", "estimated", "unknown"]


class Usage(BaseModel):
    prompt_tokens: TokenCount = 0
    completion_tokens: TokenCount = 0
    source: UsageSource = "reported"
    prompt_cache_hit_tokens: TokenCount | None = None
    prompt_cache_miss_tokens: TokenCount | None = None
    reasoning_tokens: TokenCount | None = None
    reported_total_tokens: TokenCount | None = None

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def merged(self, other: "Usage") -> "Usage":
        details = ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "reasoning_tokens", "reported_total_tokens")

        def empty(value: "Usage") -> bool:
            return value.total == 0 and value.source == "reported" and all(getattr(value, field) is None for field in details)

        def detail(field: str) -> int | None:
            left, right = getattr(self, field), getattr(other, field)
            if empty(self):
                return right
            if empty(other):
                return left
            return left + right if left is not None and right is not None else None

        priority = {"reported": 0, "estimated": 1, "unknown": 2}
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            source=max((self.source, other.source), key=priority.__getitem__),
            **{field: detail(field) for field in details},
        )


class LLMResponse(BaseModel):
    """一次模型调用归一化后的结果。所有 provider 都产出这个形状。"""

    text: str = ""
    content_is_null: StrictBool = False
    reasoning_content: StrictStr | None = Field(default=None, repr=False)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str = ""
    latency_ms: float = 0.0
    model: str = ""
    request_attempts: RequestAttempts = 1
    request_id: StrictStr | None = None

    def to_assistant_message(self) -> dict[str, Any]:
        """把模型回复转回 messages 数组里的一条 assistant 消息。

        注意：回传 tool_calls 时 arguments 必须是**字符串**（不是对象），
        否则下一轮请求会 400 —— 这是新手最常踩的坑之一。
        """
        message: dict[str, Any] = {"role": "assistant", "content": None if self.content_is_null else self.text}
        if self.reasoning_content is not None:
            message["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": call.type,
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments if call.raw_arguments is not None
                        else json.dumps(call.arguments, ensure_ascii=False, allow_nan=False),
                    },
                }
                for call in self.tool_calls
            ]
        return message
