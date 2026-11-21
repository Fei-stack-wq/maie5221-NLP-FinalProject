"""RAO 原子单元：Reason → Act → Observe。

这是整个系统的**最小可测试单元**。`agent.py` 只负责驱动它（循环、预算、验收），
不再自己写"调模型 + 跑工具"的逻辑。

为什么要把一步做成对象
----------------------
1. **可单步测试**：给定一条 mock 决策，断言它产出什么观察、扣多少预算、是否触发终止。
   非确定性系统唯一廉价的测试手段就是这个。
2. **可归因**：错误被分类成 R_（决策错）/ A_（执行错）/ O_（观察失真）/ T_（终止），
   报告里的"故障模式分析"直接按这个维度统计。
3. **可扩展**：HITL 与委派不是"塞一个假工具"，而是决策空间里的两种合法形态
   （`ask_human` / `delegate_task`），语义清晰、可持久化。

契约（改代码时不要破坏）
------------------------
- 决策闭集：`act | answer | ask_human | delegate | abort`，不接受自由文本决策。
- 一次 `act` 可含 1..N 个动作，超出 `max_actions_per_step` 的部分也要回一条观察
  （否则下一轮请求会因 tool_call_id 不匹配而 400）。
- 任何执行结果都必须变成 `Observation`；工具层不抛异常，这里也不抛。
- 历史只追加（append-only），便于重放与审计。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, PrivateAttr

from .config import Limits
from .providers import DeepSeekProvider, Provider
from .sandbox import Sandbox, SandboxError
from .tool_progress import ToolProgress
from .studio_reviews import REVIEW_REQUEST_SCHEMA
from .schemas import LLMResponse, RequestAttempts, ToolCall, ToolResult, ToolSpec, Usage, UsageSource
from .tools import ToolContext, ToolRegistry


_TEXT_TOOL_TAG = re.compile(
    r"^\s*<(?:[｜|]{1,2}DSML[｜|]{1,2}\s*(?:(?:function[_▁ ])?calls?|invoke)\b"
    r"|[｜|]tool[▁_ ]calls?[▁_ ]begin[｜|]>)", re.IGNORECASE)
_CODE_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _unstructured_tool_call(text: str) -> bool:
    """Identify provider invocation markers in prose, leaving Markdown examples intact."""
    fence = ""
    for line in text.splitlines():
        marker = _CODE_FENCE.match(line)
        if marker:
            token, rest = marker.groups()
            if not fence:
                fence = token
                continue
            if token[0] == fence[0] and len(token) >= len(fence) and not rest.strip():
                fence = ""
                continue
        if not fence and _TEXT_TOOL_TAG.match(line):
            return True
    return False

# ---------------------------------------------------------------------------
# 错误分类：报告里的失败模式就是按这些常量统计的
# ---------------------------------------------------------------------------
class ErrorClass:
    """R_ = Reason 阶段，A_ = Act 阶段，O_ = Observe 阶段，T_ = 终止判定。"""

    # Reason
    R_INVALID_DECISION = "R_INVALID_DECISION"      # 模型既没调工具也没给答复 / 输出不可解析
    R_PROVIDER_ERROR = "R_PROVIDER_ERROR"          # 模型调用失败（网络、限流、鉴权）

    # Act
    A_INVALID_ARGS = "A_INVALID_ARGS"              # 工具参数不是合法 JSON / 不符合 schema
    A_UNKNOWN_TOOL = "A_UNKNOWN_TOOL"              # 模型幻觉出不存在的工具
    A_SANDBOX_DENY = "A_SANDBOX_DENY"              # 路径越界，沙箱拒绝
    A_HUMAN_DENIED = "A_HUMAN_DENIED"              # 人类拒绝或无人可问
    A_TOOL_TIMEOUT = "A_TOOL_TIMEOUT"              # 工具超时
    A_TOOL_ERROR = "A_TOOL_ERROR"                  # 工具内部异常
    A_SKIPPED = "A_SKIPPED"                        # 超过单步动作上限，未执行
    A_UNSUPPORTED = "A_UNSUPPORTED"                # 当前形态不支持（如未接编排器时的委派）

    # Observe
    O_TRUNCATED = "O_TRUNCATED"                    # 输出被截断，信息可能不全

    # 终止
    T_ACCEPTANCE_FAILED = "T_ACCEPTANCE_FAILED"    # 验收多次未通过
    T_LOOP = "T_LOOP"                              # 检测到重复动作
    T_BUDGET = "T_BUDGET"                          # 预算耗尽
    T_ABORT = "T_ABORT"                            # 主动中止
    T_CANCEL = "T_CANCEL"                          # 用户请求停止；不执行后续动作
    T_STORE = "T_STORE"                            # 持久动作身份登记失败；不得执行工具
    T_PROTOCOL = "T_PROTOCOL"                      # 响应协议异常/截断；已收到的用量仍记录


def classify_failure(content: str) -> str:
    """把工具返回的失败文本归到错误类。这是廉价的、可解释的启发式。"""
    if "沙箱拒绝" in content:
        return ErrorClass.A_SANDBOX_DENY
    if "需要人类批准" in content or "已取消" in content or "被拒绝" in content:
        return ErrorClass.A_HUMAN_DENIED
    if "参数校验失败" in content or "缺少必填参数" in content or "未知参数" in content:
        return ErrorClass.A_INVALID_ARGS
    if "未知工具" in content:
        return ErrorClass.A_UNKNOWN_TOOL
    if "超时" in content:
        return ErrorClass.A_TOOL_TIMEOUT
    return ErrorClass.A_TOOL_ERROR


# ---------------------------------------------------------------------------
# 验收：完成判定的外部谓词（不接受"模型说完成了"）
# ---------------------------------------------------------------------------
# Re-export the compatible checker helpers; the runtime uses the four-state API.
from .acceptance import Acceptance, AcceptanceInput, accept_files, evaluate


def evaluate_acceptance(acceptance: AcceptanceInput, workspace: Path) -> tuple[bool, str]:
    """Compatibility predicate. Missing acceptance never means verified success.

    Agent uses ``acceptance.evaluate`` so failed/unverified/error remain distinct.
    """
    outcome = evaluate(
        acceptance, workspace, task_id="legacy-acceptance",
        attempt_id="legacy-attempt", objective="Legacy acceptance check",
    )
    reason = outcome.report.reason or "验收通过"
    if acceptance is None:
        reason = "no-acceptance"
    elif outcome.report.verdict == "error":
        reason = f"验收函数异常：{reason}"
    return outcome.report.verdict == "passed", reason


# ---------------------------------------------------------------------------
# 数据形状
# ---------------------------------------------------------------------------
class Action(BaseModel):
    """决策里的一个具体动作。"""

    id: str = ""
    action_id: str = ""
    type: Literal["function"] = "function"
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str | None = None
    parse_error: str | None = None

    @property
    def signature(self) -> str:
        """用于循环检测：同工具 + 同参数 = 同一个动作。"""
        payload = json.dumps(self.arguments, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(f"{self.tool}:{payload}".encode("utf-8")).hexdigest()[:16]


class Decision(BaseModel):
    """Reason 阶段的输出。闭集五选一。"""

    kind: str = "act"  # act | answer | ask_human | delegate | abort
    actions: list[Action] = Field(default_factory=list)
    answer: str = ""
    question: str = ""
    delegate: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    protocol_error: str | None = None
    unstructured_tool_call: bool = False


class Observation(BaseModel):
    """Act 结果的归一化对象。所有下游（重试、指标、记忆）都只认这个形状。"""

    step_id: str
    action_id: str | None = None
    tool_call_id: str | None = None
    source: str
    ok: bool
    content: str
    error_class: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    bytes: int = 0
    truncated: bool = False
    ts: float = 0.0

    def to_message(self, tool_call_id: str) -> dict[str, Any]:
        payload = self.content
        if self.error_class:
            payload = f"[{self.error_class}] {payload}"
        return {"role": "tool", "tool_call_id": tool_call_id, "content": payload}


class Budget(BaseModel):
    """四维预算。单调递减，父任务可向子任务切分。"""

    steps_left: int | None = Field(default_factory=lambda: Limits().max_steps or None)
    seconds_left: float | None = Field(default_factory=lambda: Limits().max_seconds or None)
    # None disables the cumulative cap. Zero is an exhausted finite allocation.
    tokens_left: int | None = Field(default_factory=lambda: Limits().max_total_tokens or None)
    _tokens_used: int = PrivateAttr(default=0)
    actions_left: int | None = Field(default_factory=lambda: Limits().max_steps * Limits().max_tool_calls_per_step or None)
    _steps_used: int = PrivateAttr(default=0)
    _actions_used: int = PrivateAttr(default=0)

    @classmethod
    def from_limits(cls, limits: Limits, *, factor: float = 1.0) -> "Budget":
        """按 Limits 造一份预算。factor < 1 用于给子任务打折。"""
        return cls(
            steps_left=max(1, int(limits.max_steps * factor)) if limits.max_steps else None,
            seconds_left=limits.max_seconds * factor if limits.max_seconds else None,
            tokens_left=int(limits.max_total_tokens * factor) if limits.max_total_tokens else None,
            actions_left=max(
                1,
                int(limits.max_steps * max(1, limits.max_tool_calls_per_step) * factor),
            ) if limits.max_steps else None,
        )

    def spend(
        self,
        *,
        steps: int = 0,
        actions: int = 0,
        tokens: int = 0,
        seconds: float = 0.0,
    ) -> "Budget":
        self._steps_used += steps
        self._actions_used += actions
        if self.steps_left is not None:
            self.steps_left -= steps
        if self.actions_left is not None:
            self.actions_left -= actions
        self._tokens_used += tokens
        if self.tokens_left is not None:
            self.tokens_left -= tokens
        if self.seconds_left is not None:
            self.seconds_left -= seconds
        return self

    @property
    def tokens_used(self) -> int:
        """Measured usage is independent of the remaining allocation."""
        return self._tokens_used

    def has_tokens(self, minimum: int) -> bool:
        """Whether a reservation or request is affordable in this allocation."""
        return self.tokens_left is None or self.tokens_left >= minimum

    @property
    def steps_used(self) -> int:
        return self._steps_used

    @property
    def actions_used(self) -> int:
        return self._actions_used

    def has_steps(self, minimum: int) -> bool:
        return self.steps_left is None or self.steps_left >= minimum

    def has_actions(self, minimum: int) -> bool:
        return self.actions_left is None or self.actions_left >= minimum

    def has_time(self, reserve: float = 0) -> bool:
        return self.seconds_left is None or self.seconds_left > reserve

    def available_seconds(self, reserve: float = 0) -> float | None:
        return None if self.seconds_left is None else self.seconds_left - reserve

    def capped_seconds(self, other: float | None) -> float | None:
        if self.seconds_left is None:
            return other
        return self.seconds_left if other is None else min(self.seconds_left, other)

    def available_tokens(self, reserve: int = 0) -> int | None:
        return None if self.tokens_left is None else self.tokens_left - reserve

    @property
    def exhausted(self) -> bool:
        return (
            not self.has_steps(1)
            or not self.has_actions(1)
            or not self.has_tokens(0)
            or not self.has_time()
        )

    def reason(self, limits: Limits | None = None) -> str:
        if not self.has_time():
            total = limits.max_seconds if limits else 0
            return f"超过时间上限 {total:.0f}s"
        if not self.has_steps(1):
            total = limits.max_steps if limits else 0
            return f"超过最大步数 {total}"
        if not self.has_actions(1):
            return "超过动作总数上限"
        if not self.has_tokens(0):
            total = limits.max_total_tokens if limits else 0
            return f"超过 token 预算 {total}"
        return "预算耗尽"

    def fork(self, ratio: float = 0.5) -> "Budget":
        """给子任务切一块预算，自己保留其余部分（默认各半）。"""
        ratio = min(max(ratio, 0.0), 1.0)
        child = Budget(
            steps_left=max(1, int(self.steps_left * ratio)) if self.steps_left is not None and self.steps_left > 1 else self.steps_left,
            seconds_left=None if self.seconds_left is None else self.seconds_left * ratio,
            tokens_left=None if self.tokens_left is None else int(self.tokens_left * ratio),
            actions_left=max(1, int(self.actions_left * ratio)) if self.actions_left is not None and self.actions_left > 1 else self.actions_left,
        )
        if self.steps_left is not None:
            self.steps_left -= child.steps_left
        if child.seconds_left is not None:
            self.seconds_left -= child.seconds_left
        if self.tokens_left is not None:
            self.tokens_left -= child.tokens_left
        if self.actions_left is not None:
            self.actions_left -= child.actions_left
        return child


class RAOState(BaseModel):
    """一步的全部输入。历史 append-only，scratch 放临时标记（重复计数、验收失败次数）。"""

    goal: str
    task_id: str
    step_index: int = 0
    history: list[dict[str, Any]] = Field(default_factory=list)
    scratch: dict[str, Any] = Field(default_factory=dict)
    _tool_progress: ToolProgress = PrivateAttr(default_factory=ToolProgress)


@dataclass(frozen=True)
class PendingPeerReview:
    """End one member's attempt with its work still private pending adjudication."""

    reason: str


@dataclass(frozen=True)
class ToolCompletion:
    """Host-authorized final text derived from authenticated tool receipts."""

    answer: str
    evidence: dict[str, Any]

    def __post_init__(self):
        if not self.answer.strip() or not self.evidence:
            raise ValueError("tool completion requires text and bound evidence")


@dataclass(frozen=True)
class WorkCheckpoint:
    """Request prefix plus the next complete batch's index in the raw transcript."""
    history_end: int
    messages: list[dict[str, Any]]

    def rebase_indices(self, excluded, measured):
        shift = len(self.messages) - self.history_end
        return ([(max(start, self.history_end) + shift, end + shift) for start, end in excluded
                 if end > self.history_end],
                {index + shift: cost for index, cost in measured.items() if index >= self.history_end})


@dataclass(frozen=True)
class BoundHandoff:
    """A workflow transition whose owner and inputs have already been decided."""

    target: str
    version: str
    state: str
    owner: str
    objective: str
    evidence_revision: str


@dataclass(frozen=True)
class StepPolicy:
    """Host-selected request mode. An empty allowlist requires a text response."""

    phase: str
    instruction: str = ""
    allowed_tools: frozenset[str] | None = None
    compact: bool = False
    token_reserve: int = 0
    seconds_reserve: float = 0
    allowed_write_paths: frozenset[str] | None = None
    required_tool: str | None = None
    reviewed_delivery: tuple[dict, ...] = ()
    bound_handoff: BoundHandoff | None = None

    def __post_init__(self) -> None:
        if self.bound_handoff and "delegate_task" not in (self.allowed_tools or ()):
            raise ValueError("a bound handoff must be authorized to delegate")
        if self.required_tool is not None and self.required_tool not in (self.allowed_tools or ()):
            raise ValueError("required_tool must be in the allowed tool set")
        if self.token_reserve < 0:
            raise ValueError("token_reserve must be non-negative")
        if not math.isfinite(self.seconds_reserve) or self.seconds_reserve < 0:
            raise ValueError("seconds_reserve must be finite and non-negative")


def policy_messages(state: RAOState, policy: StepPolicy) -> list[dict[str, Any]]:
    """Project request-only steering without growing replay history.

    Compact work retains its assignment; later requests append whole batches
    to that checkpoint. A compact closing request uses the current objective,
    authenticated human replies and the caller's current receipts.
    """
    if policy.compact:
        messages = [m for m in state.history[:1] if m.get("role") == "system"]
        if policy.phase == "work" and (context := state.scratch.get("work_context")):
            messages.append({"role": "user", "content": context})
        messages.append({"role": "user", "content": state.goal})
        for question, reply in state.scratch.get("human_replies", []):
            messages.append({"role": "user", "content": f"[用户已答复]\n问题：{question}\n答复：{reply}"})
        messages.extend(state.scratch.get("live_user_messages", ()))
        if policy.phase == "work" and state._tool_progress.current_advisory:
            messages.append({"role": "user", "content": state._tool_progress.current_advisory})
        if feedback := state.scratch.get("delivery_feedback"):
            messages.append({"role": "user", "content": feedback})
    elif checkpoint := state.scratch.get("work_checkpoint"):
        messages = [*checkpoint.messages, *state.history[checkpoint.history_end:]]
    else:
        messages = list(state.history)
    if policy.instruction:
        messages.append({"role": "user", "content": policy.instruction})
    if correction := state.scratch.get("tool_protocol_feedback"):
        messages.append({"role": "user", "content": correction})
    if policy.allowed_tools == frozenset():
        messages.append({"role": "user", "content": (
            "本轮仅提交最终文字交接。根据已有证据说明结果及限制。"
            "遇到阻塞请以 [BLOCKED] 开头写明原因。"
        )})
    return messages


class StepRecord(BaseModel):
    """一步的审计记录 —— 也就是写进 trace 的东西。"""

    step: int
    decision_kind: str = "act"
    tool_names: list[str] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    ok: bool = True
    error_class: str | None = None
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    answer: str = ""
    terminate_reason: str | None = None
    finish_reason: str = ""
    model: str = ""
    usage_source: UsageSource = "reported"
    request_attempts: RequestAttempts = 1
    request_id: str | None = None
    recoverable_output_length: bool = False
    unstructured_tool_call: bool = False
    host_handoff: dict[str, str] | None = None
    progress_advisory: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# ---------------------------------------------------------------------------
# 内置控制工具（不是普通工具：RAO 拦截它们并转成决策形态）
# ---------------------------------------------------------------------------
ASK_HUMAN_TOOL = ToolSpec(
    name="ask_human",
    description="向人类提问以澄清歧义，或请求危险操作的许可。只有在信息缺失、无法自行判断时使用。",
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "要问人类的具体问题"},
            "decision_kind": {"type": "string", "description": "本问题需要的决策类型",
                              "enum": ["business_requirement", "user_preference", "missing_user_data",
                                       "external_permission", "internal_routing", "runtime_capability"]},
        },
        "required": ["question"],
    },
)

DELEGATE_TOOL = ToolSpec(
    name="delegate_task",
    description="把一个边界清晰的子任务委派给另一个更合适的智能体。仅当系统启用了多智能体编排时有效。",
    parameters={
        "type": "object",
        "properties": {
            "objective": {"type": "string", "description": "给执行者的目标描述"},
            "assignee": {"type": "string", "description": "目标智能体名（留空则由编排器决定）"},
            "acceptance": {"type": "string", "description": "该子任务的验收标准"},
            "remaining_roles": {"type": "array", "items": {"type": "string"},
                                "description": "群组计划中本次委派之后仍需参与的角色 ID，空数组表示本角色是最后一位。"},
            "deliverable_paths": {"type": "array", "items": {"type": "string"},
                                  "description": "本次委派由该成员负责交付的具体文件相对路径。仅阅读的输入文件不计入。"},
            "review": REVIEW_REQUEST_SCHEMA,
            "recover_from": {"type": "string", "description": "接手已有受阻草稿时，填写该成员的角色 ID。"},
        },
        "required": ["objective"],
    },
)

ABORT_TOOL = ToolSpec(
    name="abort_task",
    description="放弃当前任务。仅当任务无法完成且继续尝试只会浪费预算时使用。",
    parameters={
        "type": "object",
        "properties": {"reason": {"type": "string", "description": "中止原因"}},
        "required": ["reason"],
    },
)

CONTROL_TOOLS: dict[str, ToolSpec] = {
    ASK_HUMAN_TOOL.name: ASK_HUMAN_TOOL,
    DELEGATE_TOOL.name: DELEGATE_TOOL,
    ABORT_TOOL.name: ABORT_TOOL,
}

# 给模型的系统提示（与 agent.py 的角色提示拼接使用）
RAO_SYSTEM_SUFFIX = """
## 决策方式
每一步按任务需要选择下一项动作：
1. **调用一个或多个工具**，使用结构化 tool_calls。需要同步进展时，可以在这条消息的公开正文里先告诉用户，再调用工具。
2. **直接给出最终答复**（不再调用任何工具）。这一条会触发验收检查。

## 特殊工具
- `ask_human`：信息不足或需要许可时提问。被拒绝后请换方案，不要重复问同一个问题。
- `delegate_task`：把边界清晰的子任务交给别的 agent（仅多智能体模式可用）。
- `abort_task`：确实无法完成时主动放弃，并说明卡点。

## 纪律
1. 一次只做一件事；工具报错先读错误信息再改，不要重复同样的调用。
2. 不要编造事实；不确定就在最终答复里说明缺什么。
3. 文档使用 .md 或 .txt，代码按任务选择扩展名；所有路径都是相对路径。
"""


# ---------------------------------------------------------------------------
# RAO 单元
# ---------------------------------------------------------------------------
class RAOUnit:
    """一次 Reason-Act-Observe。无状态（状态全在 RAOState 里），可单独测试。"""

    def __init__(
        self,
        *,
        name: str,
        provider: Provider,
        registry: ToolRegistry,
        sandbox: Sandbox,
        limits: Limits,
        temperature: float = 0.0,
        approve: Callable[[str], bool] | None = None,
        human_handler: Callable[[str], str | None] | None = None,
        delegate_handler: Callable[[dict[str, Any]], Observation] | None = None,
        max_actions_per_step: int = 4,
        loop_warn: int = 3,
        enable_control_tools: bool = True,
        action_recorder: Callable[[RAOState, list[Action]], list[str]] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        human_request_gate: Callable[[dict[str, Any]], str | None] | None = None,
        autonomous: bool = False,
        contains_configured_secret: Callable[[str], bool] | None = None,
    ) -> None:
        self.name = name
        self.provider = provider
        self.registry = registry
        self.sandbox = sandbox
        self.limits = limits
        self.temperature = temperature
        self.autonomous = autonomous
        self.contains_configured_secret = contains_configured_secret
        self.approve = None if autonomous else approve
        self.human_handler = None if autonomous else human_handler
        self.human_request_gate = human_request_gate
        self.delegate_handler = delegate_handler
        self.max_actions_per_step = max_actions_per_step
        self.loop_warn = loop_warn
        self.enable_control_tools = enable_control_tools
        self.delegate_spec = DELEGATE_TOOL
        self.tool_started: Callable[[int, Action], None] | None = None
        self.tool_observed: Callable[[int, Action, Observation], None] | None = None
        self.action_recorder = action_recorder
        self.should_cancel = should_cancel
        self.steering_pending: Callable[[], bool] | None = None
        self.missing_deliverables: Callable[[], list[str]] | None = None

    # ------------------------------------------------------------------ 工具
    def tool_specs(self, allowed_tools: frozenset[str] | None = None,
                   allowed_write_paths: frozenset[str] | None = None, *, include_unavailable=False) -> list[ToolSpec]:
        specs = self.registry.specs(available_only=not include_unavailable)
        if self.enable_control_tools:
            specs += [self.delegate_spec if name == "delegate_task" else spec
                      for name, spec in CONTROL_TOOLS.items() if not (self.autonomous and name == "ask_human")]
        if allowed_tools is not None:
            specs = [spec for spec in specs if spec.name in allowed_tools]
        if allowed_write_paths is None:
            return specs
        scoped = []
        for spec in specs:
            if not allowed_write_paths or spec.name not in {"write_file", "edit_file", "append_file"}:
                continue
            properties = dict(spec.parameters["properties"])
            properties["path"] = {**properties["path"], "enum": sorted(allowed_write_paths)}
            scoped.append(spec.model_copy(update={"parameters": {**spec.parameters, "properties": properties}}))
        return scoped

    # ---------------------------------------------------------------- Reason
    @staticmethod
    def _assistant_message(response: LLMResponse) -> tuple[dict[str, Any] | None, str | None]:
        """Validate the whole envelope before any action can have side effects.

        Native providers validate earlier. This additional boundary also covers
        custom providers and unchecked Pydantic construction/copy operations.
        Error messages deliberately omit the response and reasoning contents.
        """
        try:
            checked = LLMResponse.model_validate(response.model_dump(mode="python", warnings=False))
            ids = [call.id for call in checked.tool_calls]
            if any(not identity.strip() for identity in ids) or len(ids) != len(set(ids)):
                return None, "工具调用 ID 缺失或重复，未执行本轮动作。"
            if any(not call.name.strip() for call in checked.tool_calls):
                return None, "工具名称为空，未执行本轮动作。"
            if checked.content_is_null and checked.text:
                return None, "响应的空 content 标记与文本不一致。"
            return checked.to_assistant_message(), None
        except (ValueError, TypeError, AttributeError, RecursionError):
            return None, "模型响应不符合消息/工具调用协议，未执行本轮动作。"

    @staticmethod
    def _arguments(raw: str | None, supplied: dict[str, Any], error: str | None) -> tuple[dict[str, Any], str | None]:
        """Use exactly the JSON sent back in history as the executable arguments."""
        if error:
            return {}, error

        def invalid_constant(_value: str) -> None:
            raise ValueError("non-finite JSON number")

        def finite_float(encoded: str) -> float:
            number = float(encoded)
            if not math.isfinite(number):
                raise ValueError("non-finite JSON number")
            return number

        def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate JSON key")
                value[key] = item
            return value

        try:
            encoded = json.dumps(supplied, ensure_ascii=False, allow_nan=False) if raw is None else raw
            value = json.loads(encoded, parse_constant=invalid_constant,
                               parse_float=finite_float, object_pairs_hook=unique_keys)
            if not isinstance(value, dict):
                return {}, "参数不是 JSON 对象"
            return value, None
        except (ValueError, TypeError, RecursionError):
            return {}, "参数不是合法 JSON 对象"

    def decide(self, state: RAOState, request_history: list[dict[str, Any]] | None = None,
               max_output_tokens: int | None = None,
               allowed_tools: frozenset[str] | None = None,
               request_seconds: float | None = None,
               allowed_write_paths: frozenset[str] | None = None,
               required_tool: str | None = None,
               on_stream: Callable[[str, str], None] | None = None) -> tuple[Decision, LLMResponse]:
        messages = state.history if request_history is None else request_history
        tools = self.tool_specs(allowed_tools, allowed_write_paths)
        if isinstance(self.provider, DeepSeekProvider) and (max_output_tokens is not None or on_stream is not None):
            limits = {"max_output_tokens": max_output_tokens}
            if on_stream is not None:
                limits["on_stream"] = on_stream
                limits["should_cancel"] = self.should_cancel
            if required_tool is not None and self.provider.thinking == "disabled":
                limits["required_tool"] = required_tool
            if request_seconds is not None:
                limits["deadline_seconds"] = request_seconds
            response = self.provider.chat(messages, tools, temperature=self.temperature,
                                          **limits)
        else:
            response = self.provider.chat(messages, tools, temperature=self.temperature)
        decision = self.parse_response(response)
        return decision, response

    def parse_response(self, response: LLMResponse) -> Decision:
        """把模型回复归一成闭集里的一个决策。

        注意：`ask_human` / `delegate_task` / `abort_task` 在协议上仍是工具调用，
        这里只把它们标记出来，真正的语义在 Act 阶段处理（要回 tool 消息保持协议合法）。
        """
        _message, error = self._assistant_message(response)
        if error:
            return Decision(kind="act", reason=error, protocol_error=error)
        # Use the validated nested objects as well, rather than reading an
        # unchecked model_copy/model_construct payload after validating a copy.
        response = LLMResponse.model_validate(response.model_dump(mode="python", warnings=False))
        if response.finish_reason not in {"", "stop", "tool_calls"}:
            reason = "模型响应被截断或未正常结束，未执行动作且不接受为最终答复。"
            return Decision(kind="act", reason=reason, protocol_error=reason)
        if ((response.tool_calls and response.finish_reason == "stop")
                or (not response.tool_calls and response.finish_reason == "tool_calls")):
            reason = "finish_reason 与实际工具调用不一致。"
            return Decision(kind="act", reason=reason, protocol_error=reason)
        if response.text and _unstructured_tool_call(response.text):
            reason = "模型在正文中输出了工具调用标记；本轮工具未执行，不能作为最终答复。"
            return Decision(kind="act", reason=reason, protocol_error=reason, unstructured_tool_call=True)
        if response.tool_calls:
            actions = []
            for call in response.tool_calls:
                arguments, parse_error = self._arguments(call.raw_arguments, call.arguments, call.parse_error)
                actions.append(Action(
                    id=call.id,
                    type=call.type,
                    tool=call.name,
                    arguments=arguments,
                    raw_arguments=call.raw_arguments,
                    parse_error=parse_error,
                ))
            head = (response.text or "").strip().splitlines()
            return Decision(
                kind="act",
                actions=actions,
                reason=head[0][:200] if head else "",
            )

        text = (response.text or "").strip()
        if text:
            return Decision(kind="answer", answer=text)
        return Decision(kind="act", actions=[], reason="模型既没有调用工具也没有给出答复")

    # ------------------------------------------------------------------- Act
    def act(
        self, state: RAOState, decision: Decision, budget: Budget,
        allowed_tools: frozenset[str] | None = None, token_reserve: int = 0,
        allowed_write_paths: frozenset[str] | None = None,
        tool_deadline: float | None = None,
    ) -> tuple[str, list[Observation], str | None]:
        """执行决策。返回 (实际决策形态, 观察列表, 终止原因)。"""
        if tool_deadline is None and budget.seconds_left is not None:
            tool_deadline = time.monotonic() + budget.seconds_left
        observations: list[Observation] = []
        terminate: str | None = None
        step_id = f"{state.task_id}:s{state.step_index}"

        # Direct callers of act() must obey the same all-or-nothing envelope
        # check. A bad ID/type in a later call cannot follow an earlier write.
        try:
            actions = [Action.model_validate(action.model_dump(mode="python", warnings=False))
                       for action in decision.actions]
            ids = [action.id for action in actions]
            if (any(not identity.strip() for identity in ids) or len(ids) != len(set(ids))
                    or any(not action.tool.strip() for action in actions)):
                raise ValueError("invalid action identity")
        except (ValueError, TypeError, AttributeError, RecursionError):
            obs = Observation(step_id=step_id, source="provider", ok=False,
                              error_class=ErrorClass.T_PROTOCOL,
                              content="动作协议无效，未执行本轮动作。", ts=time.time())
            return "act", [obs], ErrorClass.T_PROTOCOL

        # 空动作：模型什么都没做
        if not actions:
            obs = Observation(
                step_id=step_id,
                source="system",
                ok=False,
                error_class=ErrorClass.R_INVALID_DECISION,
                content="模型既没有调用工具，也没有给出最终答复。请二选一。",
                ts=time.time(),
            )
            state.history.append({"role": "user", "content": f"[系统提示] {obs.content}"})
            return "act", [obs], None

        # Bind the whole batch before any side effect. These are durable intent
        # identities, not execution receipts or an authorization/lease decision.
        try:
            identities = (self.action_recorder(state, actions) if self.action_recorder
                          else [f"action-{uuid.uuid4().hex}" for _ in actions])
            if (len(identities) != len(actions) or len(set(identities)) != len(identities)
                    or any(not isinstance(value, str) or not value.strip() for value in identities)):
                raise ValueError("invalid action identity result")
            actions = [action.model_copy(update={"action_id": identity})
                       for action, identity in zip(actions, identities)]
        except Exception:
            observations = []
            for action in actions:
                obs = Observation(
                    step_id=step_id, tool_call_id=action.id, source=action.tool,
                    ok=False, error_class=ErrorClass.T_STORE,
                    content="动作身份持久化失败，本轮动作均未执行。", ts=time.time(),
                )
                observations.append(obs)
                state.history.append(obs.to_message(action.id))
            return "act", observations, ErrorClass.T_STORE

        kind = "act"
        repeat = state.scratch.setdefault("repeat", {"sig": "", "count": 0})
        feedback: list[dict[str, str]] = []
        budget_timer = time.perf_counter()
        # Check the actual advertised tools, including whether control handlers
        # are enabled. A mixed batch is rejected before its first side effect.
        batch_denial = ""
        for i, action in enumerate(actions):
            parsed, error = self._arguments(action.raw_arguments, action.arguments, action.parse_error)
            actions[i] = action.model_copy(update={"arguments": parsed, "parse_error": error})
        if self.autonomous or allowed_tools is not None or allowed_write_paths is not None:
            offered = {spec.name for spec in self.tool_specs(allowed_tools, allowed_write_paths)}
            if any(action.tool not in offered for action in actions):
                unavailable = sorted({action.tool for action in actions} - offered)
                batch_denial = ("本批次含当前阶段未开放的工具，全部未执行。收到：" + ", ".join(unavailable)
                                + "。当前可调用：" + (", ".join(sorted(offered)) or "无；请给出最终文字交接") + "。")
                if self.autonomous and "ask_human" in unavailable:
                    batch_denial += "本轮由团队自行决定可逆偏好并注明假设；缺少外部授权不能执行，交付可完成部分并说明限制。"
            elif allowed_write_paths is not None:
                for action in actions:
                    path = action.arguments.get("path")
                    try:
                        target = self.sandbox.relative(self.sandbox.resolve(path)) if isinstance(path, str) else None
                    except (SandboxError, OSError, ValueError, RuntimeError):
                        target = None
                    if target not in allowed_write_paths:
                        batch_denial = "本批次写入路径超出本次补交范围，全部未执行。请只写指定文件。"
                        break
            if batch_denial:
                state.scratch["policy_denials"] = state.scratch.get("policy_denials", 0) + 1
                state.scratch["policy_error"] = batch_denial
                state.scratch["delivery_feedback"] = batch_denial
            else:
                state.scratch.pop("policy_denials", None)
                if state.scratch.get("delivery_feedback") == state.scratch.pop("policy_error", None):
                    state.scratch.pop("delivery_feedback", None)

        for index, action in enumerate(actions):
            now = time.perf_counter()
            budget.spend(seconds=now - budget_timer)
            budget_timer = now
            call_id = action.id
            if self.should_cancel and self.should_cancel():
                terminate = ErrorClass.T_CANCEL
            elif not budget.has_tokens(0) or not budget.has_time():
                terminate = ErrorClass.T_BUDGET
            if terminate:
                obs = Observation(
                    step_id=step_id, source=action.tool, ok=False,
                    error_class=ErrorClass.A_SKIPPED,
                    content=("未执行：用户请求停止。" if terminate == ErrorClass.T_CANCEL
                             else "未执行：时间或 token 预算已耗尽。" if terminate == ErrorClass.T_BUDGET
                             else "未执行：前序动作已终止本轮任务。"), ts=time.time(),
                )
            elif self.steering_pending and self.steering_pending():
                # Pair all remaining call IDs before inserting the new user message.
                obs = Observation(step_id=step_id, source=action.tool, ok=False,
                                  error_class=ErrorClass.A_SKIPPED,
                                  content="用户刚补充了要求，本项操作尚未开始。请结合新消息重新决定。", ts=time.time())
            elif batch_denial or not budget.has_tokens(token_reserve):
                obs = Observation(
                    step_id=step_id, source=action.tool, ok=False, error_class=ErrorClass.A_SKIPPED,
                    content=batch_denial or "本轮剩余 token 已进入收尾预留额度，未执行动作。", ts=time.time(),
                )
            elif index >= self.max_actions_per_step:
                obs = Observation(
                    step_id=step_id,
                    source=action.tool,
                    ok=False,
                    error_class=ErrorClass.A_SKIPPED,
                    content="未执行：超过单步动作上限，请减少一次生成的数量。",
                    ts=time.time(),
                )
            elif not budget.has_actions(1):
                terminate = ErrorClass.T_BUDGET
                obs = Observation(
                    step_id=step_id, source=action.tool, ok=False,
                    error_class=ErrorClass.A_SKIPPED,
                    content="未执行：动作总数预算已耗尽。", ts=time.time(),
                )
            elif action.parse_error:
                obs = Observation(
                    step_id=step_id,
                    source=action.tool,
                    ok=False,
                    error_class=ErrorClass.A_INVALID_ARGS,
                    content=f"参数不是合法 JSON（{action.parse_error}）。请重新生成这次调用。",
                    ts=time.time(),
                )
            elif action.tool in CONTROL_TOOLS and self.enable_control_tools:
                if self.tool_started:
                    self.tool_started(state.step_index, action)
                kind, obs, terminate = self._run_control(action, state, step_id)
                if obs.ok:
                    if obs.source == "human":
                        state.scratch.setdefault("human_replies", []).append((obs.meta["question"], obs.content))
                    # The failure remains in history. A later successful call
                    # supersedes its transient instruction in compact prompts.
                    previous = state.scratch.pop("control_feedback:" + action.tool, None)
                    if previous and state.scratch.get("delivery_feedback") == previous:
                        state.scratch.pop("delivery_feedback")
                else:
                    control_error = f"[{action.tool} 未完成] {obs.content[:1000]}"
                    state.scratch["control_feedback:" + action.tool] = control_error
                    state.scratch["delivery_feedback"] = control_error
            else:
                if self.tool_started:
                    self.tool_started(state.step_index, action)
                result = self.registry.dispatch(
                    action.tool,
                    action.arguments,
                    ToolContext(
                        sandbox=self.sandbox, limits=self.limits, approve=self.approve,
                        deadline=tool_deadline, should_cancel=self.should_cancel,
                        contains_configured_secret=self.contains_configured_secret,
                    ),
                )
                obs = self._observe(result, action, step_id)
                budget.spend(actions=1)

                # Test commands can vary every round while returning exactly the
                # same findings. This is a prompt to reassess, not a verdict on
                # side effects: successful commands can be deliberately silent.
                if action.tool in {"run_python", "run_shell"} and len(obs.content) >= 64:
                    digest = hashlib.sha256(obs.content.encode("utf-8")).hexdigest()
                    previous, count = state.scratch.get("command_result", (None, 0))
                    count = count + 1 if digest == previous else 1
                    state.scratch["command_result"] = (digest, count)
                    if count == self.loop_warn:
                        feedback.append({"role": "user", "content": (
                            f"[测试进展提醒] 连续 {count} 次命令调用返回了相同的输出。"
                            "检查子进程真实退出码和剩余失败断言；已有证据足够时直接交付，"
                            "仍有失败时先定位原因再复测，避免反复改写和运行同一项测试。"
                        )})
                else:
                    state.scratch.pop("command_result", None)

                # 循环检测：同一动作连续重复
                signature = action.signature
                if signature == repeat["sig"]:
                    repeat["count"] += 1
                else:
                    repeat["sig"], repeat["count"] = signature, 1
                if repeat["count"] >= self.loop_warn:
                    feedback.append(
                        {
                            "role": "user",
                            "content": (
                                f"[系统提示] 同一个动作（{action.tool}）已连续重复 "
                                f"{repeat['count']} 次。请换一个策略，或说明你为什么卡住。"
                            ),
                        }
                    )

            obs = obs.model_copy(update={"action_id": action.action_id, "tool_call_id": call_id})
            if self.tool_observed:
                self.tool_observed(state.step_index, action, obs)
            observations.append(obs)
            state.history.append(obs.to_message(call_id))

        # Keep all tool results adjacent to their assistant batch. Interleaving
        # a user warning before the final result breaks subsequent requests.
        state.history.extend(feedback)
        budget.spend(seconds=time.perf_counter() - budget_timer)
        return kind, observations, terminate

    # --------------------------------------------------------------- Observe
    def _observe(self, result: ToolResult, action: Action, step_id: str) -> Observation:
        artifacts = list(result.meta.get("artifacts") or [])
        if (result.ok and not artifacts and action.tool not in {"read_file", "read_lines", "read_source", "list_dir", "find_files", "search_files"}
                and isinstance(result.meta.get("path"), str)):
            artifacts = [result.meta["path"]]
        error_class = None if result.ok else classify_failure(result.content)
        return Observation(
            step_id=step_id,
            source=action.tool,
            ok=result.ok,
            content=result.content,
            error_class=error_class,
            artifacts=artifacts,
            meta=dict(result.meta),
            bytes=len(result.content.encode("utf-8")),
            truncated=bool(result.meta.get("truncated")) or "[已截断" in result.content,
            ts=time.time(),
        )

    # ------------------------------------------------------------- 控制工具
    def _run_control(
        self, action: Action, state: RAOState, step_id: str
    ) -> tuple[str, Observation, str | None]:
        if action.tool == "ask_human":
            return self._ask_human(action, state, step_id)
        if action.tool == "delegate_task":
            return self._delegate(action, step_id)
        if action.tool == "abort_task":
            reason = str(action.arguments.get("reason") or "模型主动中止")
            obs = Observation(
                step_id=step_id,
                source="abort",
                ok=False,
                error_class=ErrorClass.T_ABORT,
                content=f"任务已中止：{reason}",
                ts=time.time(),
            )
            return "abort", obs, ErrorClass.T_ABORT
        # 理论不可达，保底
        return "act", Observation(
            step_id=step_id, source=action.tool, ok=False,
            error_class=ErrorClass.A_UNKNOWN_TOOL, content=f"未知控制工具：{action.tool}",
            ts=time.time(),
        ), None

    def _ask_human(
        self, action: Action, state: RAOState, step_id: str
    ) -> tuple[str, Observation, str | None]:
        question = str(action.arguments.get("question") or "")
        if self.human_request_gate and (reason := self.human_request_gate(action.arguments)):
            return "ask_human", Observation(
                step_id=step_id, source="human_gate", ok=False, error_class=ErrorClass.A_HUMAN_DENIED,
                content=reason, ts=time.time(),
            ), None
        decision_kind = action.arguments.get("decision_kind")
        confirmed = None
        if decision_kind in ("business_requirement", "user_preference") and question:
            confirmed = state.scratch.setdefault("confirmed_clarifications", {}).setdefault(decision_kind, {})
            if question in confirmed:
                # This is a host observation about an existing decision. It
                # does not call the human channel, copy an answer as new input,
                # or confer any approval. Exact text is scoped to this attempt.
                return "ask_human", Observation(
                    step_id=step_id, source="human_gate", ok=False, error_class=ErrorClass.A_HUMAN_DENIED,
                    content="本次执行中用户已经回答了这条相同的业务问题。请依照先前的答复继续分工或交付。"
                            "如需新的授权，请使用 external_permission 并说明具体操作。",
                    meta={"answered_action": confirmed[question]}, ts=time.time(),
                ), None
        if self.human_handler is None:
            obs = Observation(
                step_id=step_id,
                source="human",
                ok=False,
                error_class=ErrorClass.A_HUMAN_DENIED,
                content=(
                    "当前没有人类可应答（无人值守模式）。请基于已有信息自行判断并继续，"
                    "不要再调用 ask_human。"
                ),
                ts=time.time(),
            )
            return "ask_human", obs, None
        try:
            reply = self.human_handler(question)
        except Exception as exc:  # 人类通道故障不应带走任务
            reply = None
            question = f"{question}（人类通道异常：{type(exc).__name__}）"
        if reply is None:
            obs = Observation(
                step_id=step_id,
                source="human",
                ok=False,
                error_class=ErrorClass.A_HUMAN_DENIED,
                content="人类没有回答（或拒绝了该请求）。请换一种方案，不要重复询问。",
                ts=time.time(),
            )
            return "ask_human", obs, None
        if confirmed is not None:
            confirmed[question] = action.action_id or action.id
        obs = Observation(
            step_id=step_id,
            source="human",
            ok=True,
            content=str(reply),
            meta={"question": question},
            ts=time.time(),
        )
        return "ask_human", obs, None

    def _delegate(
        self, action: Action, step_id: str
    ) -> tuple[str, Observation, str | None]:
        payload = dict(action.arguments or {})
        # This linkage is runtime-owned; never trust a model-supplied identity.
        payload["__runtime_action_id"] = action.action_id
        if self.delegate_handler is None:
            obs = Observation(
                step_id=step_id,
                source="delegate",
                ok=False,
                error_class=ErrorClass.A_UNSUPPORTED,
                content=(
                    "当前未启用多智能体编排，无法委派。请自己完成该子任务，"
                    "或在最终答复里说明这部分需要谁来接手。"
                ),
                ts=time.time(),
            )
            return "delegate", obs, None
        try:
            obs = self.delegate_handler(payload)
        except Exception as exc:
            obs = Observation(
                step_id=step_id,
                source="delegate",
                ok=False,
                error_class=ErrorClass.A_TOOL_ERROR,
                content=f"委派失败：{type(exc).__name__}: {exc}",
                ts=time.time(),
            )
        return "delegate", obs, None

    # ------------------------------------------------------------------ Step
    def step(self, state: RAOState, budget: Budget,
             request_history: list[dict[str, Any]] | None = None,
             max_output_tokens: int | None = None,
             allowed_tools: frozenset[str] | None = None, token_reserve: int = 0,
             request_seconds: float | None = None,
             allowed_write_paths: frozenset[str] | None = None,
             required_tool: str | None = None, bound_handoff: BoundHandoff | None = None,
             on_stream: Callable[[str, str], None] | None = None) -> StepRecord:
        """完整的一步：Reason → Act → Observe，并把观察追加进历史。"""
        started = time.perf_counter()
        request_seconds = budget.capped_seconds(request_seconds)
        tool_deadline = None if request_seconds is None else time.monotonic() + request_seconds
        state.step_index += 1

        if self.should_cancel and self.should_cancel():
            return StepRecord(step=state.step_index, ok=False, decision_kind="abort",
                              error_class=ErrorClass.T_CANCEL, terminate_reason=ErrorClass.T_CANCEL)
        if not budget.has_tokens(0) or not budget.has_time():
            return StepRecord(step=state.step_index, ok=False,
                              error_class=ErrorClass.T_BUDGET, terminate_reason=ErrorClass.T_BUDGET)
        decision, response = self.decide(state, request_history, max_output_tokens=max_output_tokens,
                                         allowed_tools=allowed_tools, request_seconds=request_seconds, required_tool=required_tool,
                                         allowed_write_paths=allowed_write_paths,
                                         **({"on_stream": on_stream} if on_stream is not None else {}))
        try:
            raw_usage = getattr(response, "usage", None)
            if isinstance(raw_usage, Usage):
                raw_usage = raw_usage.model_dump(mode="python", warnings=False)
            usage = Usage.model_validate(raw_usage)
        except (ValueError, TypeError, AttributeError):
            usage = Usage(source="unknown")
        budget.spend(tokens=usage.total)
        budget.spend(seconds=time.perf_counter() - started)

        terminate: str | None = None
        observations: list[Observation] = []
        kind = decision.kind

        message, envelope_error = self._assistant_message(response)
        if message is not None:
            state.history.append(message)
        protocol_error = decision.protocol_error or envelope_error
        cancelled = bool(self.should_cancel and self.should_cancel())
        budget_overrun = not budget.has_tokens(0) or not budget.has_time()
        host_handoff = None
        if protocol_error or cancelled or budget_overrun:
            terminate = (ErrorClass.T_CANCEL if cancelled else ErrorClass.T_BUDGET
                         if budget_overrun else ErrorClass.T_PROTOCOL)
            observations.append(Observation(
                step_id=f"{state.task_id}:s{state.step_index}", source="provider", ok=False,
                error_class=terminate,
                content=("用户请求停止。" if cancelled else "时间或 token 预算已耗尽。"
                         if budget_overrun else protocol_error), ts=time.time(),
            ))
            # A well-formed but incomplete response can be retained as history;
            # none of its calls may execute, but each still needs a tool result.
            for call in (message or {}).get("tool_calls", []):
                obs = Observation(
                    step_id=f"{state.task_id}:s{state.step_index}", source=call["function"]["name"],
                    ok=False, error_class=ErrorClass.A_SKIPPED,
                    content=("未执行：用户请求停止。" if cancelled
                             else "未执行：时间或 token 预算已耗尽。" if budget_overrun
                             else "未执行：本轮模型响应未正常结束。"), ts=time.time(),
                )
                observations.append(obs)
                state.history.append(obs.to_message(call["id"]))
        elif (bound_handoff and "delegate_task" in (allowed_tools or ())
              and bound_handoff != state.scratch.get("last_host_handoff")
              and (decision.kind == "answer" or (decision.actions and
                   any(a.tool not in {"delegate_task", "ask_human"} for a in decision.actions)))):
            # Keep the provider's actual envelope intact. Pair any superseded
            # requests before recording a separately identified runtime action.
            for call in (message or {}).get("tool_calls", []):
                skipped = Observation(step_id=f"{state.task_id}:s{state.step_index}",
                                      tool_call_id=call["id"], source=call["function"]["name"],
                                      ok=False, error_class=ErrorClass.A_SKIPPED,
                                      content="已有绑定的分工，本次请求由宿主交接取代。")
                observations.append(skipped)
                state.history.append(skipped.to_message(call["id"]))
            action = Action(id="host_" + uuid.uuid4().hex, tool="delegate_task",
                            arguments={"assignee": bound_handoff.owner, "objective": bound_handoff.objective})
            host_handoff = {"target": bound_handoff.target, "version": bound_handoff.version,
                            "state": bound_handoff.state, "owner": bound_handoff.owner, "call_id": action.id}
            state.scratch["last_host_handoff"] = bound_handoff
            state.history.append({"role": "user", "content": "[宿主调度] 已登记的验收决定需要下一位成员执行。"})
            handoff_call = ToolCall(id=action.id, name=action.tool, arguments=action.arguments)
            state.history.append(LLMResponse(content_is_null=True, reasoning_content="",
                                             tool_calls=[handoff_call]).to_assistant_message())
            decision = Decision(kind="act", actions=[action])
            kind, dispatched, terminate = self.act(state, decision, budget, allowed_tools, token_reserve,
                                                   allowed_write_paths, tool_deadline)
            observations.extend(dispatched)
        elif decision.kind != "answer":
            # Commit the validated public prose before a tool can block or
            # delegate another task. Tool arguments and reasoning are separate
            # protocol fields and never enter this channel.
            if on_stream is not None and decision.actions and response.text.strip():
                on_stream("progress", response.text)
            kind, observations, terminate = self.act(state, decision, budget, allowed_tools, token_reserve,
                                                    allowed_write_paths, tool_deadline)

        # Superseded proposals stay in the transcript for protocol pairing.
        # The step verdict belongs to the action that the runtime executed.
        outcomes = dispatched if host_handoff else observations
        advisory = None
        if not host_handoff and not terminate and outcomes:
            advisory = state._tool_progress.observe_batch(
                decision.actions, outcomes, self.loop_warn, self.missing_deliverables)
            if advisory:
                state.history.append({"role": "user", "content": advisory})
        failed = [obs for obs in outcomes if not obs.ok]
        return StepRecord(
            step=state.step_index,
            decision_kind=kind,
            host_handoff=host_handoff,
            progress_advisory=advisory,
            tool_names=[obs.source for obs in observations],
            observations=observations,
            ok=not failed,
            error_class=failed[0].error_class if failed else None,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            tool_calls=sum(
                1
                for obs in observations
                if obs.source not in CONTROL_TOOLS and obs.error_class is None
            ),
            answer=decision.answer if decision.kind == "answer" and not protocol_error and not cancelled and not budget_overrun else "",
            terminate_reason=terminate,
            finish_reason=getattr(response, "finish_reason", "") if isinstance(getattr(response, "finish_reason", ""), str) else "",
            model=getattr(response, "model", "") if isinstance(getattr(response, "model", ""), str) else "",
            usage_source=usage.source,
            request_attempts=(getattr(response, "request_attempts", 1)
                              if type(getattr(response, "request_attempts", 1)) is int
                              and getattr(response, "request_attempts", 1) >= 1 else 1),
            request_id=getattr(response, "request_id", None) if isinstance(getattr(response, "request_id", None), str) else None,
            recoverable_output_length=(not cancelled and not budget_overrun and envelope_error is None
                                       and getattr(response, "finish_reason", "") == "length"),
            unstructured_tool_call=decision.unstructured_tool_call and not cancelled and not budget_overrun,
        )


__all__ = [
    "Acceptance",
    "Action",
    "Budget",
    "CONTROL_TOOLS",
    "Decision",
    "ErrorClass",
    "Observation",
    "RAO_SYSTEM_SUFFIX",
    "RAOUnit",
    "RAOState",
    "StepRecord",
    "StepPolicy",
    "policy_messages",
    "accept_files",
    "classify_failure",
    "evaluate_acceptance",
]
