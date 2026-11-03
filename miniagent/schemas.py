"""系统的数据契约（pydantic 模型）。

这些模型就是"边界"：LLM 的输出在这里被校验，工具的输入输出在这里被定型，
trace 在这里被序列化。改这里的字段要谨慎 —— 它决定了整个系统的形状。

对应课程要求：
- ToolSpec / ToolCall  -> 2.3 行动空间（工具调用协议）
- RunResult            -> 评估指标（完成率、耗时）
- Usage                -> 成本与 token 追踪
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal[
    "completed",          # 通过验收（无验收函数时为软完成）
    "failed_acceptance",  # 模型说完成，但验收多次未通过
    "limit_exceeded",     # 预算耗尽 / 检测到循环
    "aborted",            # 模型主动放弃（abort_task）
    "error",              # 模型调用失败或运行时异常
]


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

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str = ""
    """模型返回的原始字符串。参数解析失败时要把它记录下来，方便排查。"""

    parse_error: str | None = None
    """模型偶尔会返回非法 JSON —— 这不是异常，是一个必须优雅处理的事件。"""


class ToolResult(BaseModel):
    """工具执行结果。永远不要抛异常给 agent 循环，错误也是一种"结果"。"""

    ok: bool
    content: str
    meta: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def success(cls, content: str, **meta: Any) -> "ToolResult":
        return cls(ok=True, content=content, meta=meta)

    @classmethod
    def failure(cls, content: str, **meta: Any) -> "ToolResult":
        return cls(ok=False, content=content, meta=meta)


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def merged(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )


class LLMResponse(BaseModel):
    """一次模型调用归一化后的结果。所有 provider 都产出这个形状。"""

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str = ""
    latency_ms: float = 0.0
    model: str = ""

    def to_assistant_message(self) -> dict[str, Any]:
        """把模型回复转回 messages 数组里的一条 assistant 消息。

        注意：回传 tool_calls 时 arguments 必须是**字符串**（不是对象），
        否则下一轮请求会 400 —— 这是新手最常踩的坑之一。
        """
        message: dict[str, Any] = {"role": "assistant", "content": self.text or ""}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments
                        or json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        return message


class RunResult(BaseModel):
    """一个任务的最终结果 —— 也就是指标统计的基本单位。"""

    task_id: str
    agent: str
    status: Status = "error"
    answer: str = ""
    error: str = ""
    duration_ms: float = 0.0
    steps: int = 0
    tool_calls: int = 0
    usage: Usage = Field(default_factory=Usage)
    sandbox: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"
