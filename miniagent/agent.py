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

import copy
import time
import uuid
from typing import Callable

from .config import Settings
from .shell_tool import CommandSessions
from .compaction import RollingCompactor
from .request_window import fit_history, output_headroom
from .context_window import ContextLimitError, TaskTokenLimitError, UsageAnchor
from .acceptance import AcceptanceInput, bind, evaluate
from .metrics import Tracer
from .providers import Provider, ProviderError, provider_configuration
from .rao import (
    Budget,
    ErrorClass,
    RAO_SYSTEM_SUFFIX,
    RAOState,
    RAOUnit,
    StepPolicy,
    StepRecord,
    policy_messages,
)
from .sandbox import Sandbox
from .schemas import RunResult, Usage
from .tools import ToolRegistry
from .task_service import ExecutionHandle, TaskService
from .task_store import StoreError

SYSTEM_PROMPT = (
    """你是「{agent}」，一个多智能体系统中的成员。

你有一个私有工作区（沙箱），所有文件路径都是相对路径；禁止使用绝对路径或 ".."。
文档使用 Markdown (.md) 或纯文本 (.txt)，代码按任务选择扩展名。
控制单轮输出长度，较大产物按任务拆成多个较小文件。write_file 会覆盖同名文件。
修改已有代码优先使用 edit_file；大文件分块创建后使用 append_file 续写。
已有项目先用 find_files / search_files 定位入口、定义、调用点与测试，用 read_lines 按行查看相关实现。
使用 run_shell 在项目目录执行其原生构建和测试，run_python 可做结构化数据核对。网页流程可用 run_browser 直接操作浏览器控件并验证结果。
在工作副本中局部修改并复测，依据实际退出码、断言结果和文件交接。
不要假装自己执行过工具。
"""
    + RAO_SYSTEM_SUFFIX
)


COORDINATOR_PROMPT = """你是本项目群的技术负责人。通过本轮 API 提供的工具协调团队。
用户的目标由团队共同完成。负责人根据所选角色的专长决定分工，使用 delegate_task 交给合适的成员。
你会收到项目目录清单、成员的发布记录、检查证据和剩余问题。规划时可以查阅与分工有关的片段。
已有足够背景就委派，收到完整交付后向用户总结实际成果、使用方法和验证情况。
业务偏好或外部授权确需用户决定时，通过 ask_human 询问。宿主提供的统计、执行权限和团队路由由工作室处理。
历史公开消息中的工具说法只代表当时的上下文。每个回合以当轮提供的工具定义和宿主回执为准。
"""


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
        task_service: TaskService | None = None,
        should_cancel: Callable[[], bool] | None = None,
        human_request_gate: Callable[[dict], str | None] | None = None,
    ) -> None:
        self.name = name
        self.task_service = task_service
        self.provider = provider
        self.registry = registry
        self.sandbox = sandbox
        self.settings = settings
        self.tracer = tracer
        self.approve = approve
        self.max_acceptance_retries = max_acceptance_retries
        self.should_cancel = should_cancel
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
            human_request_gate=human_request_gate,
            delegate_handler=delegate_handler,  # type: ignore[arg-type]
            max_actions_per_step=limits.max_tool_calls_per_step,
            should_cancel=should_cancel,
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
        execution: ExecutionHandle | None = None,
        *,
        conversation_context: str = "",
        system_prompt: str | None = None,
        before_answer: Callable[[], str | None] | None = None,
        step_policy: Callable[[RAOState, Budget], StepPolicy] | None = None,
        preflight_handoff: Callable[[RAOState, Budget], StepPolicy] | None = None,
    ) -> RunResult:
        """跑完一个任务。

        ``budget`` 由调度器注入（编排一个多子任务计划时共享大局预算）；
        不传则按 ``settings.limits`` 新建。
        ``on_step`` 是给调度器的钩子：每步结束立刻回调，用于写黑板、检查依赖。
        """
        task_id = task_id or (execution.task.task_id if execution else f"task-{uuid.uuid4().hex}")
        durable_task = None
        if execution is not None and self.task_service is None:
            raise ValueError("an execution handle requires its TaskService")
        if self.task_service is not None:
            if execution is None:
                if parent_task_id is not None or (root_id is not None and root_id != task_id):
                    raise ValueError("persisted children must be registered and claimed by their coordinator")
                registered = self.task_service.create(
                    task_id=task_id, creation_key=f"root:{task_id}", objective=task,
                    owner=self.name, assignee=self.name,
                )
                execution = self.task_service.start(
                    task_id, expected_revision=registered.revision,
                    context={"objective": task, "agent": self.name,
                             "workspace": self.sandbox.root.as_posix()},
                    attempt_id=attempt_id,
                )
            durable_task = self.task_service.store.get(task_id)
            if (durable_task != execution.task or durable_task.status != "running"
                    or execution.task.objective != task
                    or execution.attempt.task_id != task_id
                    or durable_task.current_attempt_id != execution.attempt.attempt_id
                    or (attempt_id is not None and attempt_id != execution.attempt.attempt_id)
                    or (root_id is not None and root_id != durable_task.root_id)
                    or (parent_task_id is not None and parent_task_id != durable_task.parent_id)):
                raise StoreError("execution handle does not match claimed work", code="conflict")
            stored_attempt = self.task_service.store.get_record("AttemptRecord", execution.attempt.attempt_id)
            stored_run = self.task_service.store.get_record("RunRecord", execution.run.run_id)
            if (stored_attempt != execution.attempt or stored_attempt.state != "running"
                    or stored_run != execution.run or stored_run.state != "running"
                    or stored_run.task_id != task_id or stored_run.attempt_id != stored_attempt.attempt_id):
                raise StoreError("execution task/attempt/run binding is not active", code="conflict")
            # A claim handle is consumed once. Reusing it in another executor
            # cannot replay the same action identities and duplicate effects.
            self.task_service.attach_execution(execution)
            root_id, parent_task_id = durable_task.root_id, durable_task.parent_id
            attempt_id = execution.attempt.attempt_id

            def record_actions(state, actions):
                rows = self.task_service.action_identities(
                    task_id, attempt_id=attempt_id, run_id=execution.run.run_id,
                    step=state.step_index, calls=[
                        {"tool_call_id": action.id, "operation": action.tool,
                         "arguments": action.arguments, "raw_arguments": action.raw_arguments}
                        for action in actions
                    ],
                )
                return [row["action_id"] for row in rows]

            self.unit.action_recorder = record_actions
        else:
            self.unit.action_recorder = None
        attempt_id = attempt_id or f"attempt-{uuid.uuid4().hex}"
        run_id = execution.run.run_id if execution else f"run-{uuid.uuid4().hex}"
        identity = {
            "schema_version": 2, "task_id": task_id,
            "root_id": root_id or task_id, "attempt_id": attempt_id, "run_id": run_id,
            "parent_task_id": parent_task_id,
            "level": "subtask" if root_id is not None and root_id != task_id else "goal",
        }
        limits = self.settings.limits
        started = time.perf_counter()

        history = [{"role": "system", "content": system_prompt or SYSTEM_PROMPT.format(agent=self.name)}]
        if conversation_context:
            # Past public turns are background. The new request remains the durable objective.
            history.append({"role": "user", "content": conversation_context})
        history.append({"role": "user", "content": task})
        state = RAOState(
            goal=task,
            task_id=task_id,
            history=history,
        )
        budget = budget or Budget.from_limits(limits)
        initial_seconds = min(budget.seconds_left, limits.max_seconds)
        initial_steps, initial_tokens = budget.steps_left, budget.tokens_left
        initial_actions = budget.actions_left
        excluded_ranges: list[tuple[int, int]] = []
        compactor = RollingCompactor()
        overflow_used = False
        retry_envelope = None
        output_recoveries = 0
        last_omitted = 0
        warned_budgets: set[str] = set()
        usage_anchor: UsageAnchor | None = None
        reported_assistant_tokens: dict[int, tuple[dict, int]] = {}

        usage = Usage()
        tool_calls = 0
        acceptance_failures = 0
        answer_guard_retries = 0
        reviewed_close_retried = False
        rejected_text_turns = 0
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
                provider_config=provider_configuration(self.provider),
            )

        step_started = None

        def cancellation_observed() -> bool:
            if not self.should_cancel or not self.should_cancel():
                return False
            if self.tracer:
                self.tracer.event("cancel_observed", **identity, step=state.step_index)
            return True

        preflight_handoff_used = False
        # A malformed work action and a malformed final handoff have distinct
        # protocols. Each may be corrected once, within the original budget.
        corrected_protocols: set[str] = set()

        def budget_reason() -> str:
            if budget.seconds_left <= 0:
                return f"超过时间上限 {initial_seconds:.0f}s"
            if not budget.has_tokens(0):
                return f"超过 token 预算 {initial_tokens}"
            if not budget.has_steps(1):
                return f"超过最大步数 {initial_steps}"
            if not budget.has_actions(1):
                return f"超过动作总数上限 {initial_actions}"
            return budget.reason(limits)

        owned_commands = self.sandbox.command_sessions is None
        if owned_commands:
            self.sandbox.command_sessions = CommandSessions()
        try:
            acceptance = bind(acceptance, self.sandbox.root, task_id=task_id, objective=task)
            while True:
                budget.seconds_left = initial_seconds - (time.perf_counter() - started)
                if cancellation_observed():
                    status, error = "aborted", "用户请求停止"
                    break
                policy = step_policy(state, budget) if step_policy else StepPolicy(phase="work")
                offered = self.unit.tool_specs(policy.allowed_tools, policy.allowed_write_paths)
                text_only = step_policy is not None and not offered
                # The final text needs a model step, but consumes no tool actions.
                if (not budget.has_steps(1) or not budget.has_tokens(0) or budget.seconds_left <= 0
                        or (not budget.has_actions(1) and not text_only)):
                    error = budget_reason()
                    break

                for dimension, remaining, initial in (
                    ("steps", budget.steps_left, initial_steps),
                    ("tokens", budget.tokens_left, initial_tokens),
                ):
                    if remaining is not None and dimension not in warned_budgets and remaining <= max(1, initial * 0.2):
                        warned_budgets.add(dimension)
                        state.history.append({
                            "role": "user", "content": (
                                f"[预算提醒] 当前任务剩余{remaining} {'步' if dimension == 'steps' else 'tokens'}。"
                                "请优先完成必要修复、验证与交付，避免重复读取或重写整个文件；"
                                "仍须如实说明未完成项。"
                            ),
                        })
                        if self.tracer:
                            self.tracer.event("budget_warning", **identity, dimension=dimension,
                                              remaining=remaining, initial=initial)
                output_reserve = provider_configuration(self.provider).get("max_output_tokens")
                if not isinstance(output_reserve, int):
                    output_reserve = self.settings.max_output_tokens
                def project(policy, offered):
                    schemas = [spec.to_api() for spec in offered]
                    available = budget.available_tokens(policy.token_reserve)
                    view, pruned = fit_history(
                        policy_messages(state, policy), tool_schemas=schemas,
                        window=self.settings.context_window_tokens, ceiling=output_reserve,
                        replay_target=retry_envelope[0] // 2 if retry_envelope else None,
                        excluded_ranges=None if policy.compact else excluded_ranges,
                        usage_anchor=None if policy.compact else usage_anchor,
                        reported_assistant_tokens=None if policy.compact else reported_assistant_tokens,
                        available=available, steps_left=budget.steps_left,
                    )
                    return schemas, available, view, pruned

                try:
                    tool_schemas, spendable_tokens, projection, pruned = project(policy, offered)
                except TaskTokenLimitError as exc:
                    if (preflight_handoff is None or preflight_handoff_used or text_only
                            or not policy.token_reserve):
                        raise
                    closing = preflight_handoff(state, budget)
                    if not closing.compact or closing.allowed_tools != frozenset() or closing.token_reserve:
                        raise
                    preflight_handoff_used = True
                    if self.tracer:
                        self.tracer.event("handoff_preflight_recovery", **identity, step=state.step_index+1,
                                          work_phase=policy.phase, work_tokens=budget.available_tokens(policy.token_reserve),
                                          tokens_remaining=budget.tokens_left, reason=str(exc))
                    policy, offered, text_only = closing, [], True
                    tool_schemas, spendable_tokens, projection, pruned = project(policy, offered)
                if not policy.compact:
                    summary = compactor.update(
                        policy_messages(state, policy), projection, self.provider,
                        context_window_tokens=self.settings.context_window_tokens,
                        available_tokens=spendable_tokens,
                        seconds_left=initial_seconds - (time.perf_counter() - started) - policy.seconds_reserve,
                        force=retry_envelope is not None,
                    )
                    projection = summary.projection
                    if summary.attempted:
                        usage = usage.merged(summary.usage)
                        budget.spend(tokens=summary.usage.total)
                        spendable_tokens = budget.available_tokens(policy.token_reserve)
                        if self.tracer:
                            self.tracer.event("compaction_usage", **identity, call_id=f"summary-{uuid.uuid4().hex}",
                                              step=state.step_index + 1, transport=self.provider.transport,
                                              status=summary.status, covered_groups=summary.covered_groups,
                                              new_groups=summary.new_groups, usage=summary.usage.model_dump(),
                                              usage_source=summary.usage.source, request_attempts=summary.request_attempts)
                window = self.settings.context_window_tokens
                capacity = window - max(256, window // 100) - projection.estimated_input_tokens
                request_output_limit = min(output_reserve, capacity)
                if spendable_tokens is not None:
                    request_output_limit = min(request_output_limit, spendable_tokens - projection.estimated_input_tokens)
                    if request_output_limit <= 0:
                        raise TaskTokenLimitError("累计 token 预算不足以支付当前请求及答复。")
                if retry_envelope:
                    old_input, old_output = retry_envelope
                    request_output_limit = min(request_output_limit, max(1, old_output // 2))
                    if projection.estimated_input_tokens + request_output_limit >= old_input + old_output:
                        raise ContextLimitError("服务端报告上下文过长，保留当前用户要求后无法再缩小请求。")
                if request_output_limit <= 0:
                    raise ContextLimitError("保留当前指令后没有可用的单次输出空间。")
                if projection.omitted_groups != last_omitted:
                    if self.tracer:
                        self.tracer.event(
                            "context_compacted", **identity, step=state.step_index + 1,
                            strategy="semantic_summary" if not policy.compact and summary.status in {"applied", "cached"} else "deterministic_omission", omitted_groups=projection.omitted_groups,
                            omitted_messages=projection.omitted_messages,
                            original_input_tokens=projection.original_input_tokens,
                            estimated_input_tokens=projection.estimated_input_tokens,
                            input_estimate_source=projection.estimate_source,
                        )
                    last_omitted = projection.omitted_groups
                budget.seconds_left = initial_seconds - (time.perf_counter() - started)
                if budget.seconds_left <= policy.seconds_reserve:
                    error = budget_reason() if budget.seconds_left <= 0 else "本轮请求已无可用时间，剩余时间已预留给收尾。"
                    break
                budget.spend(steps=1)
                step_started = time.perf_counter()
                if self.tracer:
                    self.tracer.event(
                        "step_start", **identity, step=state.step_index + 1,
                        phase=policy.phase, compact=policy.compact, tokens_reserved=policy.token_reserve,
                        seconds_reserved=policy.seconds_reserve,
                        offered_tools=[spec.name for spec in offered], requested_tool=policy.required_tool,
                        allowed_write_paths=sorted(policy.allowed_write_paths) if policy.allowed_write_paths is not None else None,
                        provider_config=provider_configuration(self.provider),
                        seconds_remaining=round(max(0, budget.seconds_left), 2),
                        tokens_remaining=budget.tokens_left, tokens_used=budget.tokens_used,
                        steps_remaining=budget.steps_left,
                        context_window_tokens=self.settings.context_window_tokens,
                        output_reserve_tokens=output_headroom(window, output_reserve),
                        output_ceiling_tokens=output_reserve, tool_results_pruned=pruned,
                        request_output_limit=request_output_limit,
                        estimated_input_tokens=projection.estimated_input_tokens,
                        input_limit_tokens=projection.input_limit,
                        input_estimate_source=projection.estimate_source,
                    )
                history_start = len(state.history)
                state.scratch.pop("tool_protocol_feedback", None)
                try:
                    record = self.unit.step(
                        state, budget, request_history=projection.messages, max_output_tokens=request_output_limit,
                        allowed_tools=policy.allowed_tools, token_reserve=policy.token_reserve,
                        allowed_write_paths=policy.allowed_write_paths, required_tool=policy.required_tool,
                        request_seconds=budget.seconds_left - policy.seconds_reserve if step_policy else None,
                    )
                except ProviderError as exc:
                    if (exc.code != "context_length_exceeded" or overflow_used or not budget.has_steps(1)
                            or len(state.history) != history_start or cancellation_observed()):
                        raise
                    overflow_used = True
                    retry_envelope = (projection.estimated_input_tokens, request_output_limit)
                    usage_anchor = None
                    if exc.attempts:
                        usage = usage.merged(Usage(source="unknown"))
                    if self.tracer:
                        self.tracer.event("provider_error", **identity, call_id=f"error-{uuid.uuid4().hex}",
                                          step=state.step_index, code=exc.code, http_status=exc.status_code,
                                          transport=self.provider.transport, request_attempts=exc.attempts,
                                          usage_source="unknown", recovery="shrink_and_retry")
                    continue
                retry_envelope = None
                usage = usage.merged(
                    Usage(prompt_tokens=record.prompt_tokens,
                          completion_tokens=record.completion_tokens,
                          source="unknown" if record.request_attempts > 1 else record.usage_source)
                )
                tool_calls += record.tool_calls
                self._trace_step(identity, record, request_output_limit)
                if (record.usage_source == "reported" and record.terminate_reason is None
                        and len(state.history) > history_start
                        and state.history[history_start].get("role") == "assistant"):
                    assistant = copy.deepcopy(state.history[history_start])
                    reported_assistant_tokens[history_start] = (assistant, record.completion_tokens)
                    usage_anchor = UsageAnchor(
                        messages=projection.messages + [assistant], tool_schemas=tool_schemas,
                        measured_tokens=record.total_tokens,
                    )
                if on_step is not None:
                    on_step(record, state)

                if record.terminate_reason == ErrorClass.T_CANCEL or cancellation_observed():
                    if record.terminate_reason == ErrorClass.T_CANCEL and self.tracer:
                        self.tracer.event("cancel_observed", **identity, step=state.step_index)
                    status, error = "aborted", "用户请求停止"
                    break
                budget.seconds_left = initial_seconds - (time.perf_counter() - started)
                if record.terminate_reason == ErrorClass.T_BUDGET or not budget.has_tokens(0) or budget.seconds_left <= 0:
                    status, error = "limit_exceeded", budget_reason()
                    break
                if record.unstructured_tool_call:
                    protocol = "text" if text_only else "tools"
                    already_corrected = protocol in corrected_protocols
                    can_retry = not already_corrected and budget.has_steps(1) and budget.has_tokens(1)
                    if self.tracer:
                        self.tracer.event("tool_protocol_correction", **identity, step=record.step,
                                          retry=can_retry, phase=policy.phase, protocol=protocol)
                    if not can_retry:
                        status = "error" if already_corrected else "limit_exceeded"
                        error = ("纠正后仍在正文中输出工具调用；未执行该调用，无法确认交接。"
                                 if already_corrected else "模型在正文中输出工具调用，剩余额度不足以纠正。")
                        break
                    # Keep the malformed response for auditing and exclude the
                    # entire unexecuted batch (including any skipped tool replies).
                    excluded_ranges.append((history_start, len(state.history)))
                    state.scratch["tool_protocol_feedback"] = (
                        "[工具协议纠正] 上一轮正文中的调用标记未执行。需要操作且本轮提供了工具时，"
                        "请通过 API 的结构化 tool_calls 发出实际调用。若本轮仅能用文字交接，"
                        "请基于已有证据报告结果或以 [BLOCKED] 说明剩余工作。请勿在正文中书写调用标记。"
                    )
                    corrected_protocols.add(protocol)
                    continue
                if record.recoverable_output_length:
                    if output_recoveries < self.settings.max_output_recovery_attempts and not budget.exhausted:
                        output_recoveries += 1
                        # Preserve the audit history but never forward an incomplete
                        # assistant/tool envelope, or re-execute an earlier success.
                        excluded_ranges.append((history_start, len(state.history)))
                        state.history.append({
                            "role": "user", "content": (
                                "[输出截断恢复] 上次响应达到单次输出上限，该轮工具均未执行，"
                                "该不完整响应未纳入本次请求。更早已成功执行的动作仍有效，不要重复执行。"
                                "请缩小当前动作：使用 edit_file 局部修改，或 write_file 后分批 append_file；"
                                "每次只生成一个较小且完整的工具调用。若在思考中耗尽输出预算，"
                                "请简化本轮计划，先完成一个可验证的小步骤。"
                            ),
                        })
                        if self.tracer:
                            self.tracer.event(
                                "output_recovery", **identity, step=record.step,
                                attempt=output_recoveries, maximum=self.settings.max_output_recovery_attempts,
                                finish_reason=record.finish_reason,
                                omitted_response_messages=len(state.history) - history_start - 1,
                            )
                        continue
                    if self.tracer:
                        self.tracer.event(
                            "output_recovery_exhausted", **identity, step=record.step,
                            attempts=output_recoveries, maximum=self.settings.max_output_recovery_attempts,
                            budget_exhausted=budget.exhausted,
                        )
                # ① 终止原因（循环 / 主动放弃）
                if record.terminate_reason:
                    error, status = self._terminate(
                        record.terminate_reason, finish_reason=record.finish_reason,
                        max_output_tokens=request_output_limit,
                    )
                    break

                if text_only and record.decision_kind != "answer":
                    rejected_text_turns += 1
                    if rejected_text_turns > 1:
                        status, error = "error", "收尾阶段连续两次未收到有效的模型文字交接。"
                        break
                    continue
                rejected_text_turns = 0

                # ② 模型给出最终答复 → 必须过验收
                if record.decision_kind == "answer":
                    if step_policy is not None and record.answer.lstrip().startswith("[BLOCKED]"):
                        if policy.reviewed_delivery and not self.sandbox.command_sessions.pending_reason():
                            retry = (not reviewed_close_retried and budget.has_steps(1)
                                     and budget.has_tokens(1) and budget.seconds_left > 0)
                            if self.tracer:
                                self.tracer.event("reviewed_delivery_blocked", **identity, step=record.step,
                                                  action="request_clarification" if retry else "preserve_block",
                                                  receipts=list(policy.reviewed_delivery))
                            if retry:
                                reviewed_close_retried = True
                                state.scratch["delivery_feedback"] = (
                                    "[宿主结案核对] 你刚刚报了阻塞。当前文件的独立验收已成功发布，"
                                    "请根据下列版本回执重新汇总。如果另有真实阻塞，请保留 [BLOCKED] "
                                    "并具体说明超出这些标准的哪项请求尚未满足。")
                                continue
                        answer = record.answer
                        status, error = "error", answer
                        break
                    reason = self.sandbox.command_sessions.pending_reason()
                    if not reason and before_answer is not None:
                        reason = before_answer()
                    if reason:
                        answer_guard_retries += 1
                        if self.tracer:
                            self.tracer.event("delivery_blocked", **identity, reason=reason)
                        if answer_guard_retries > 1:
                            status, error = "error", reason
                            break
                        state.history.append({"role": "user", "content": reason})
                        state.scratch["delivery_feedback"] = reason
                        continue
                    answer = record.answer
                    if durable_task is not None:
                        durable_task = self.task_service.transition(
                            task_id, expected_revision=durable_task.revision, status="verifying",
                        )
                    evaluation = evaluate(
                        acceptance, self.sandbox.root, task_id=task_id,
                        attempt_id=attempt_id, objective=task,
                    )
                    acceptance_spec = evaluation.spec
                    acceptance_report = evaluation.report
                    payload = evaluation.trace_payload()
                    acceptance_evidence = payload.get("receipts", [])
                    if durable_task is not None:
                        durable_task = self.task_service.record_acceptance(
                            task_id, expected_revision=durable_task.revision,
                            spec=acceptance_spec, report=acceptance_report,
                            receipts=acceptance_evidence,
                        )
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
                    if durable_task is not None:
                        durable_task = self.task_service.transition(
                            task_id, expected_revision=durable_task.revision, status="running",
                        )
                    # 打回一次：把验收意见作为人类反馈注入，再给模型一次机会
                    state.scratch["delivery_feedback"] = f"[验收未通过] {why}"
                    state.history.append(
                        {
                            "role": "user",
                            "content": (
                                f"[验收未通过] {why}\n"
                                "请修正产物后重新给出最终答复；不要重复刚才的做法。"
                            ),
                        }
                    )
        except TaskTokenLimitError as exc:
            status, error = "limit_exceeded", str(exc)
            if self.tracer:
                self.tracer.event("token_budget_preflight", **identity, step=state.step_index + 1,
                                  tokens_remaining=budget.tokens_left, reason=error)
        except ContextLimitError as exc:
            status, error = "limit_exceeded", f"上下文空间检查未通过：{exc}（T_CONTEXT）"
            if self.tracer:
                self.tracer.event("context_limit", **identity, step=state.step_index,
                                  context_window_tokens=self.settings.context_window_tokens,
                                  reason=str(exc))
        except StoreError:
            # Persistence conflicts are not model errors and must not be hidden
            # behind a stale success or an unconditional overwrite.
            raise
        except ProviderError as exc:
            if self.tracer:
                self.tracer.event(
                    "provider_error", **identity, call_id=f"error-{uuid.uuid4().hex}",
                    transport=self.provider.transport,
                    code=exc.code, http_status=exc.status_code,
                    request_attempts=exc.attempts, usage_source="unknown",
                    step=state.step_index, retryable=exc.retryable,
                    latency_ms=round((time.perf_counter() - step_started) * 1000, 2)
                    if step_started is not None else None,
                )
            if exc.attempts:
                usage = usage.merged(Usage(source="unknown"))
            if cancellation_observed():
                status, error = "aborted", "用户请求停止"
            else:
                status, error = "error", f"模型调用失败：{exc}"
        except Exception as exc:  # noqa: BLE001 - 循环本身绝不能把进程带走
            status, error = "error", f"{type(exc).__name__}: {exc}"
        finally:
            if owned_commands:
                self.sandbox.command_sessions.close()
                self.sandbox.command_sessions = None

        if durable_task is not None:
            persistent_status = (status if status in {"completed", "unverified"}
                                 else "cancelled" if status == "aborted" else "failed")
            durable_task = self.task_service.finish(
                task_id, expected_revision=durable_task.revision,
                status=persistent_status, error=error,
                spec=acceptance_spec, report=acceptance_report, receipts=acceptance_evidence,
            )
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        result = RunResult(
            task_id=task_id,
            attempt_id=attempt_id,
            run_id=run_id,
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
    def _terminate(reason: str, *, finish_reason: str = "",
                   max_output_tokens: int | None = None) -> tuple[str, str]:
        """把 RAO 的终止原因映射为 (error, status)。"""
        if reason == ErrorClass.T_STORE:
            return "动作身份持久化失败，本轮未执行工具（T_STORE）", "error"
        if reason == ErrorClass.T_PROTOCOL:
            if finish_reason == "length":
                ceiling = f"（max_output_tokens={max_output_tokens}）" if max_output_tokens else ""
                return (
                    f"模型输出达到单次调用上限{ceiling}，响应被截断（finish_reason=length）；"
                    "本轮工具未执行。请缩小单次产出或调整输出上限后重新运行（T_PROTOCOL）", "error"
                )
            return "模型响应协议不完整或异常终止（T_PROTOCOL）", "error"
        if reason == ErrorClass.T_ABORT:
            return "任务被主动中止", "aborted"
        if reason == ErrorClass.T_CANCEL:
            return "用户请求停止", "aborted"
        if reason == ErrorClass.T_LOOP:
            return "检测到重复动作，已终止（T_LOOP）", "limit_exceeded"
        return f"终止原因：{reason}", "limit_exceeded"

    def _trace_step(self, identity: dict, record: StepRecord, request_output_limit: int) -> None:
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
            max_output_tokens=request_output_limit,
        )
        for obs in record.observations:
            command_metrics = {}
            if obs.source in {"run_shell", "run_python", "run_browser", "poll_process"}:
                metrics = ("exit_code", "elapsed_ms", "timed_out", "timeout_s", "cwd",
                           "stdout_bytes", "stderr_bytes", "truncated", "cancelled", "process_id", "process_state")
                command_metrics = {key: obs.meta[key] for key in metrics if key in obs.meta}
            self.tracer.event(
                "tool_result",
                **identity,
                step=record.step,
                tool=obs.source,
                action_id=obs.action_id,
                tool_call_id=obs.tool_call_id,
                ok=obs.ok,
                error_class=obs.error_class,
                artifacts=obs.artifacts,
                preview=obs.content[:200],
                **command_metrics,
            )
