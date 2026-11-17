"""Agent 运行时（L1）：驱动 RAO 单元，负责预算、验收与终止。

职责划分：
- ``rao.py`` 负责**一步**（Reason → Act → Observe）。
- 这个文件负责**一路**（循环、预算、验收、终态映射、trace）。

课程要求里几乎所有概念都长在这个循环上：
  - 2.1b 自主委派 -> RAO 的 ``delegate_task`` 决策（等编排器接管）
  - 2.1c HITL     -> RAO 的 ``ask_human`` 决策 + 工具层的 ``approve`` 回调
  - 2.2a 记忆演化 -> 循环开始前检索记忆注入，结束后写入
  - 评估指标      -> 每步 token/耗时/错误分类都进 tracer
"""

from __future__ import annotations

import time
import uuid
from typing import Callable

from .config import Settings
from .acceptance import AcceptanceInput, bind, evaluate
from .metrics import Tracer
from .providers import Provider, ProviderError
from .rao import (
    Budget,
    ErrorClass,
    RAO_SYSTEM_SUFFIX,
    RAOState,
    RAOUnit,
    StepRecord,
)
from .sandbox import Sandbox
from .schemas import RunResult, Usage
from .tools import ToolRegistry

SYSTEM_PROMPT = (
    """你是「{agent}」，一个多智能体系统中的成员。

你有一个私有工作区（沙箱），所有文件路径都是相对路径；禁止使用绝对路径或 ".."。
产出文档只用 Markdown (.md) 或纯文本 (.txt)。不要假装自己执行过工具。
"""
    + RAO_SYSTEM_SUFFIX
)


class Agent:
    """一个智能体的运行时。一个 Agent 实例只跑一个任务（状态不复用）。"""

    def __init__(
        self,
        name: str,
        provider: Provider,
        registry: ToolRegistry,
        sandbox: Sandbox,
        settings: Settings,
        tracer: Tracer | None = None,
        approve: Callable[[str], bool] | None = None,
        human_handler: Callable[[str], str | None] | None = None,
        delegate_handler: Callable[[dict], object] | None = None,
        max_acceptance_retries: int = 1,
    ) -> None:
        self.name = name
        self.provider = provider
        self.registry = registry
        self.sandbox = sandbox
        self.settings = settings
        self.tracer = tracer
        self.approve = approve
        self.max_acceptance_retries = max_acceptance_retries
        limits = settings.limits
        self.unit = RAOUnit(
            name=name,
            provider=provider,
            registry=registry,
            sandbox=sandbox,
            limits=limits,
            temperature=settings.temperature,
            approve=approve,
            human_handler=human_handler,
            delegate_handler=delegate_handler,  # type: ignore[arg-type]
            max_actions_per_step=limits.max_tool_calls_per_step,
        )

    # ------------------------------------------------------------------
    def run(
        self,
        task: str,
        task_id: str | None = None,
        acceptance: AcceptanceInput = None,
        budget: Budget | None = None,
        on_step: Callable[[StepRecord, RAOState], None] | None = None,
        root_id: str | None = None,
        parent_task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> RunResult:
        """跑完一个任务。

        ``budget`` 由调度器注入（编排一个多子任务计划时共享大局预算）；
        不传则按 ``settings.limits`` 新建。
        ``on_step`` 是给调度器的钩子：每步结束立刻回调，用于写黑板、检查依赖。
        """
        task_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        attempt_id = attempt_id or f"attempt-{uuid.uuid4().hex}"
        identity = {
            "schema_version": 2, "task_id": task_id,
            "root_id": root_id or task_id, "attempt_id": attempt_id,
            "parent_task_id": parent_task_id,
            "level": "subtask" if root_id is not None and root_id != task_id else "goal",
        }
        limits = self.settings.limits
        started = time.perf_counter()

        state = RAOState(
            goal=task,
            task_id=task_id,
            history=[
                {"role": "system", "content": SYSTEM_PROMPT.format(agent=self.name)},
                {"role": "user", "content": task},
            ],
        )
        budget = budget or Budget(
            steps_left=limits.max_steps,
            seconds_left=limits.max_seconds,
            tokens_left=limits.max_total_tokens,
            actions_left=limits.max_steps * max(1, limits.max_tool_calls_per_step),
        )

        usage = Usage()
        tool_calls = 0
        acceptance_failures = 0
        status = "limit_exceeded"
        answer = ""
        error = ""
        acceptance_spec = None
        acceptance_report = None
        acceptance_evidence = []

        if self.tracer:
            self.tracer.event("task_registered", **identity, agent=self.name, task=task)
            self.tracer.event(
                "task_start",
                **identity,
                agent=self.name,
                task=task,
                sandbox=self.sandbox.root.as_posix(),
                acceptance="custom" if acceptance else "none",
            )

        try:
            acceptance = bind(acceptance, self.sandbox.root, task_id=task_id, objective=task)
            while True:
                budget.seconds_left = limits.max_seconds - (time.perf_counter() - started)
                if budget.exhausted:
                    error = budget.reason(limits)
                    break

                budget.spend(steps=1)
                record = self.unit.step(state, budget)
                usage = usage.merged(
                    Usage(prompt_tokens=record.prompt_tokens,
                          completion_tokens=record.completion_tokens,
                          source="unknown" if record.request_attempts > 1 else record.usage_source)
                )
                tool_calls += record.tool_calls
                self._trace_step(identity, record)
                if on_step is not None:
                    on_step(record, state)

                # ① 终止原因（循环 / 主动放弃）
                if record.terminate_reason:
                    error, status = self._terminate(record.terminate_reason)
                    break

                # ② 模型给出最终答复 → 必须过验收
                if record.decision_kind == "answer":
                    answer = record.answer
                    evaluation = evaluate(
                        acceptance, self.sandbox.root, task_id=task_id,
                        attempt_id=attempt_id, objective=task,
                    )
                    acceptance_spec = evaluation.spec
                    acceptance_report = evaluation.report
                    payload = evaluation.trace_payload()
                    acceptance_evidence = payload.get("receipts", [])
                    verdict = acceptance_report.verdict
                    why = acceptance_report.reason or "验收通过"
                    if self.tracer:
                        self.tracer.event(
                            "acceptance",
                            **identity,
                            step=record.step,
                            ok=verdict == "passed",
                            reason=why,
                            **payload,
                        )
                    if verdict == "passed":
                        status = "completed"
                        break
                    if verdict in {"unverified", "error"}:
                        status, error = verdict, why
                        break
                    acceptance_failures += 1
                    if acceptance_failures > self.max_acceptance_retries:
                        status = "failed_acceptance"
                        error = f"{ErrorClass.T_ACCEPTANCE_FAILED}: {why}"
                        break
                    # 打回一次：把验收意见作为人类反馈注入，再给模型一次机会
                    state.history.append(
                        {
                            "role": "user",
                            "content": (
                                f"[验收未通过] {why}\n"
                                "请修正产物后重新给出最终答复；不要重复刚才的做法。"
                            ),
                        }
                    )
        except ProviderError as exc:
            if self.tracer:
                self.tracer.event(
                    "provider_error", **identity, call_id=f"error-{uuid.uuid4().hex}",
                    transport=self.provider.transport,
                    code=exc.code, http_status=exc.status_code,
                    request_attempts=exc.attempts, usage_source="unknown",
                )
            if exc.attempts:
                usage = usage.merged(Usage(source="unknown"))
            status, error = "error", f"模型调用失败：{exc}"
        except Exception as exc:  # noqa: BLE001 - 循环本身绝不能把进程带走
            status, error = "error", f"{type(exc).__name__}: {exc}"

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        result = RunResult(
            task_id=task_id,
            attempt_id=attempt_id,
            agent=self.name,
            status=status,  # type: ignore[arg-type]
            answer=answer,
            error=error,
            duration_ms=duration_ms,
            steps=state.step_index,
            tool_calls=tool_calls,
            usage=usage,
            sandbox=self.sandbox.root.as_posix(),
            acceptance_spec=acceptance_spec,
            acceptance_report=acceptance_report,
            acceptance_evidence=acceptance_evidence,
        )

        if self.tracer:
            self.tracer.event(
                "task_end",
                **identity,
                agent=self.name,
                status=result.status,
                duration_ms=result.duration_ms,
                steps=result.steps,
                tool_calls=result.tool_calls,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                usage_source=usage.source,
                error=result.error,
                acceptance_spec=acceptance_spec.model_dump(mode="json") if acceptance_spec else None,
                acceptance_report=acceptance_report.model_dump(mode="json") if acceptance_report else None,
                acceptance_evidence=acceptance_evidence,
            )
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _terminate(reason: str) -> tuple[str, str]:
        """把 RAO 的终止原因映射为 (error, status)。"""
        if reason == ErrorClass.T_PROTOCOL:
            return "模型响应协议不完整或异常终止（T_PROTOCOL）", "error"
        if reason == ErrorClass.T_ABORT:
            return "任务被主动中止", "aborted"
        if reason == ErrorClass.T_LOOP:
            return "检测到重复动作，已终止（T_LOOP）", "limit_exceeded"
        return f"终止原因：{reason}", "limit_exceeded"

    def _trace_step(self, identity: dict, record: StepRecord) -> None:
        if not self.tracer:
            return
        self.tracer.event(
            "step",
            **identity,
            step=record.step,
            decision=record.decision_kind,
            tools=record.tool_names,
            ok=record.ok,
            error_class=record.error_class,
            latency_ms=record.latency_ms,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            transport=self.provider.transport,
            usage_source=record.usage_source,
            finish_reason=record.finish_reason,
            model=record.model,
            request_attempts=record.request_attempts,
            request_id=record.request_id,
        )
        for obs in record.observations:
            self.tracer.event(
                "tool_result",
                **identity,
                step=record.step,
                tool=obs.source,
                ok=obs.ok,
                error_class=obs.error_class,
                artifacts=obs.artifacts,
                preview=obs.content[:200],
            )
