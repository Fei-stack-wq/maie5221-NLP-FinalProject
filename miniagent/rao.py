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
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from .config import Limits
from .providers import Provider
from .sandbox import Sandbox
from .schemas import LLMResponse, ToolResult, ToolSpec
from .tools import ToolContext, ToolRegistry

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
Acceptance = Callable[[Path], "tuple[bool, str]"]


def evaluate_acceptance(acceptance: Acceptance | None, workspace: Path) -> tuple[bool, str]:
    """执行验收函数。没给验收函数时退化为"软完成"（由调用方在报告里说明）。"""
    if acceptance is None:
        return True, "no-acceptance"
    try:
        ok, reason = acceptance(workspace)
    except Exception as exc:  # 验收函数自己崩了，算不通过，但不能把循环带走
        return False, f"验收函数异常：{type(exc).__name__}: {exc}"
    return bool(ok), str(reason)


def accept_files(*names: str, min_bytes: int = 20, min_lines: int = 1) -> Acceptance:
    """最常用的一类验收：产物存在、非空、有基本结构。

    用法::

        acceptance = accept_files("notes.md")
        agent.run("写一份笔记", acceptance=acceptance)
    """

    def check(workspace: Path) -> tuple[bool, str]:
        missing: list[str] = []
        thin: list[str] = []
        for name in names:
            path = workspace / name
            if not path.exists() or not path.is_file():
                missing.append(name)
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if path.stat().st_size < min_bytes or len(text.splitlines()) < min_lines:
                thin.append(name)
        if missing:
            return False, f"缺少产物：{', '.join(missing)}"
        if thin:
            return False, f"产物内容过少：{', '.join(thin)}"
        return True, "产物齐全"

    return check


# ---------------------------------------------------------------------------
# 数据形状
# ---------------------------------------------------------------------------
class Action(BaseModel):
    """决策里的一个具体动作。"""

    id: str = ""
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str = ""
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


class Observation(BaseModel):
    """Act 结果的归一化对象。所有下游（重试、指标、记忆）都只认这个形状。"""

    step_id: str
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

    steps_left: int = 12
    seconds_left: float = 120.0
    tokens_left: int = 200_000
    actions_left: int = 48

    @classmethod
    def from_limits(cls, limits: Limits, *, factor: float = 1.0) -> "Budget":
        """按 Limits 造一份预算。factor < 1 用于给子任务打折。"""
        return cls(
            steps_left=max(1, int(limits.max_steps * factor)),
            seconds_left=limits.max_seconds * factor,
            tokens_left=int(limits.max_total_tokens * factor),
            actions_left=max(
                1,
                int(limits.max_steps * max(1, limits.max_tool_calls_per_step) * factor),
            ),
        )

    def spend(
        self,
        *,
        steps: int = 0,
        actions: int = 0,
        tokens: int = 0,
        seconds: float = 0.0,
    ) -> "Budget":
        self.steps_left -= steps
        self.actions_left -= actions
        self.tokens_left -= tokens
        self.seconds_left -= seconds
        return self

    @property
    def exhausted(self) -> bool:
        return (
            self.steps_left <= 0
            or self.actions_left <= 0
            or self.tokens_left < 0
            or self.seconds_left <= 0
        )

    def reason(self, limits: Limits | None = None) -> str:
        if self.seconds_left <= 0:
            total = limits.max_seconds if limits else 0
            return f"超过时间上限 {total:.0f}s"
        if self.steps_left <= 0:
            total = limits.max_steps if limits else 0
            return f"超过最大步数 {total}"
        if self.actions_left <= 0:
            return "超过动作总数上限"
        if self.tokens_left < 0:
            total = limits.max_total_tokens if limits else 0
            return f"超过 token 预算 {total}"
        return "预算耗尽"

    def fork(self, ratio: float = 0.5) -> "Budget":
        """给子任务切一块预算，自己保留其余部分（默认各半）。"""
        ratio = min(max(ratio, 0.0), 1.0)
        child = Budget(
            steps_left=max(1, int(self.steps_left * ratio)) if self.steps_left > 1 else self.steps_left,
            seconds_left=self.seconds_left * ratio,
            tokens_left=int(self.tokens_left * ratio),
            actions_left=max(1, int(self.actions_left * ratio)),
        )
        self.steps_left -= child.steps_left
        self.seconds_left -= child.seconds_left
        self.tokens_left -= child.tokens_left
        self.actions_left -= child.actions_left
        return child


class RAOState(BaseModel):
    """一步的全部输入。历史 append-only，scratch 放临时标记（重复计数、验收失败次数）。"""

    goal: str
    task_id: str
    step_index: int = 0
    history: list[dict[str, Any]] = Field(default_factory=list)
    scratch: dict[str, Any] = Field(default_factory=dict)


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
        "properties": {"question": {"type": "string", "description": "要问人类的具体问题"}},
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
每一步你只有两种选择，必须二选一：
1. **调用一个或多个工具**（下面的工具清单），我会执行并把结果返回给你。
2. **直接给出最终答复**（不再调用任何工具）。这一条会触发验收检查。

## 特殊工具
- `ask_human`：信息不足或需要许可时提问。被拒绝后请换方案，不要重复问同一个问题。
- `delegate_task`：把边界清晰的子任务交给别的 agent（仅多智能体模式可用）。
- `abort_task`：确实无法完成时主动放弃，并说明卡点。

## 纪律
1. 一次只做一件事；工具报错先读错误信息再改，不要重复同样的调用。
2. 不要编造事实；不确定就在最终答复里说明缺什么。
3. 产出文档只用 .md 或 .txt；所有路径都是相对路径。
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
        loop_stop: int = 5,
        enable_control_tools: bool = True,
    ) -> None:
        self.name = name
        self.provider = provider
        self.registry = registry
        self.sandbox = sandbox
        self.limits = limits
        self.temperature = temperature
        self.approve = approve
        self.human_handler = human_handler
        self.delegate_handler = delegate_handler
        self.max_actions_per_step = max_actions_per_step
        self.loop_warn = loop_warn
        self.loop_stop = loop_stop
        self.enable_control_tools = enable_control_tools

    # ------------------------------------------------------------------ 工具
    def tool_specs(self) -> list[ToolSpec]:
        specs = self.registry.specs()
        if self.enable_control_tools:
            specs += list(CONTROL_TOOLS.values())
        return specs

    # ---------------------------------------------------------------- Reason
    def decide(self, state: RAOState) -> tuple[Decision, LLMResponse]:
        response = self.provider.chat(
            state.history, self.tool_specs(), temperature=self.temperature
        )
        return self.parse_response(response), response

    def parse_response(self, response: LLMResponse) -> Decision:
        """把模型回复归一成闭集里的一个决策。

        注意：`ask_human` / `delegate_task` / `abort_task` 在协议上仍是工具调用，
        这里只把它们标记出来，真正的语义在 Act 阶段处理（要回 tool 消息保持协议合法）。
        """
        if response.tool_calls:
            actions = [
                Action(
                    id=call.id,
                    tool=call.name,
                    arguments=call.arguments,
                    raw_arguments=call.raw_arguments,
                    parse_error=call.parse_error,
                )
                for call in response.tool_calls
            ]
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
        self, state: RAOState, decision: Decision, budget: Budget
    ) -> tuple[str, list[Observation], str | None]:
        """执行决策。返回 (实际决策形态, 观察列表, 终止原因)。"""
        observations: list[Observation] = []
        terminate: str | None = None
        step_id = f"{state.task_id}:s{state.step_index}"

        # 空动作：模型什么都没做
        if not decision.actions:
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

        kind = "act"
        repeat = state.scratch.setdefault("repeat", {"sig": "", "count": 0})

        for index, action in enumerate(decision.actions):
            call_id = action.id or f"{step_id}:a{index}"
            if index >= self.max_actions_per_step:
                obs = Observation(
                    step_id=step_id,
                    source=action.tool,
                    ok=False,
                    error_class=ErrorClass.A_SKIPPED,
                    content="未执行：超过单步动作上限，请减少一次生成的数量。",
                    ts=time.time(),
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
                kind, obs, terminate = self._run_control(action, state, step_id)
            else:
                result = self.registry.dispatch(
                    action.tool,
                    action.arguments,
                    ToolContext(
                        sandbox=self.sandbox, limits=self.limits, approve=self.approve
                    ),
                )
                obs = self._observe(result, action, step_id)
                budget.spend(actions=1)

                # 循环检测：同一动作连续重复
                signature = action.signature
                if signature == repeat["sig"]:
                    repeat["count"] += 1
                else:
                    repeat["sig"], repeat["count"] = signature, 1
                if repeat["count"] >= self.loop_stop:
                    terminate = ErrorClass.T_LOOP
                elif repeat["count"] >= self.loop_warn:
                    state.history.append(
                        {
                            "role": "user",
                            "content": (
                                f"[系统提示] 同一个动作（{action.tool}）已连续重复 "
                                f"{repeat['count']} 次。请换一个策略，或说明你为什么卡住。"
                            ),
                        }
                    )

            observations.append(obs)
            if call_id:
                state.history.append(obs.to_message(call_id))
            if terminate:
                break

        return kind, observations, terminate

    # --------------------------------------------------------------- Observe
    def _observe(self, result: ToolResult, action: Action, step_id: str) -> Observation:
        artifacts = list(result.meta.get("artifacts") or [])
        if not artifacts and isinstance(result.meta.get("path"), str):
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
            truncated="[已截断" in result.content,
            ts=time.time(),
        )

    # ------------------------------------------------------------- 控制工具
    def _run_control(
        self, action: Action, state: RAOState, step_id: str
    ) -> tuple[str, Observation, str | None]:
        if action.tool == "ask_human":
            return self._ask_human(action, step_id)
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
        self, action: Action, step_id: str
    ) -> tuple[str, Observation, str | None]:
        question = str(action.arguments.get("question") or "")
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
    def step(self, state: RAOState, budget: Budget) -> StepRecord:
        """完整的一步：Reason → Act → Observe，并把观察追加进历史。"""
        started = time.perf_counter()
        state.step_index += 1

        decision, response = self.decide(state)
        budget.spend(tokens=response.usage.total)

        terminate: str | None = None
        observations: list[Observation] = []
        kind = decision.kind

        if decision.kind == "answer":
            state.history.append({"role": "assistant", "content": decision.answer})
        else:
            state.history.append(response.to_assistant_message())
            kind, observations, terminate = self.act(state, decision, budget)

        failed = [obs for obs in observations if not obs.ok]
        return StepRecord(
            step=state.step_index,
            decision_kind=kind,
            tool_names=[obs.source for obs in observations],
            observations=observations,
            ok=not failed,
            error_class=failed[0].error_class if failed else None,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            tool_calls=sum(
                1
                for obs in observations
                if obs.source not in CONTROL_TOOLS and obs.error_class is None
            ),
            answer=decision.answer if decision.kind == "answer" else "",
            terminate_reason=terminate,
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
    "accept_files",
    "classify_failure",
    "evaluate_acceptance",
]
