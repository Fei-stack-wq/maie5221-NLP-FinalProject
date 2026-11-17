"""编排器（L2/L3）：把一句目标变成子任务图，并按拓扑调度执行。

两种调度实现（同一个接口，先 S1 再 S2）：

=========  ==========================  ==========================================
调度器      行为                        取舍
=========  ==========================  ==========================================
serial     一次只跑一个子任务           零竞态、顺序确定、指标最干净（先做）
layered    按拓扑层分批，层内并发       墙钟更短；要处理 trace 交织与预算竞争
=========  ==========================  ==========================================

三条设计约束（与 docs/architecture.md §4.3 一致）：

1. **规划是一等公民**：LLM 只产出结构化计划，校验（唯一性/无环/成员存在）通过后
   才执行；失败重试一次，仍失败则**降级为单任务**（降级路径必须有，否则模型一抖就全盘失败）。
2. **执行不黑盒**：调 ``Agent.run(budget=..., on_step=...)`` 注入预算与逐步回调，
   每一步都能写黑板、检查依赖、按 ``error_class`` 决定重试还是放弃。
3. **失败是局部的**：只把**依赖它**的下游标 blocked，无关分支继续跑 —— 全局 fail-fast
   会让完成率凭空掉一大截，而完成率是直接评分项。

并发提示：``ScriptedProvider`` 带游标，**不是线程安全的**。layered 模式下要么用真实
provider，要么用无状态/加锁的 provider（测试里用 TaskAwareProvider），或通过
``agent_factory`` 给每个子任务注入独立实例。
"""

from __future__ import annotations

import json
import re
import threading
import time
import tempfile
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from pydantic import BaseModel, Field

from .agent import Agent
from .acceptance import AcceptanceInput, bind, evaluate
from .acceptance_evidence import validate_evidence
from .bus import MessageBus
from .config import Settings
from .contracts.acceptance import AcceptanceReport, AcceptanceSpec, validate_report
from .metrics import Tracer
from .providers import Provider
from .rao import Budget, ErrorClass, Observation
from .sandbox import Sandbox
from .schemas import RunResult
from .tools import build_default_registry

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
class AgentCard(BaseModel):
    """能力卡：编排器据此决定"这个子任务派给谁"。"""

    name: str
    skills: list[str] = Field(default_factory=list)
    description: str = ""

    def matches(self, text: str) -> int:
        lowered = text.lower()
        return sum(1 for skill in self.skills if skill and skill.lower() in lowered)


class SubTask(BaseModel):
    id: str
    """Plan-local ID. task_id identifies the same logical task across its traces."""
    task_id: str = ""
    root_id: str = ""
    attempt_id: str = ""
    parent_id: str | None = None
    """Global parent task ID; planned tasks have the root as parent."""
    objective: str
    assignee: str = "worker"
    deps: list[str] = Field(default_factory=list)
    acceptance: str = ""
    """自然语言验收标准（给人看、也喂给执行者）。代码级验收由 acceptances 注入。"""

    status: str = "pending"
    """pending / running / done / failed / error / unverified / blocked / skipped"""

    result: str = ""
    error: str = ""
    steps: int = 0
    tool_calls: int = 0
    duration_ms: float = 0.0
    tokens: int = 0
    workspace: str = ""
    execution_status: str = ""
    acceptance_report: AcceptanceReport | None = None
    acceptance_spec: AcceptanceSpec | None = None
    acceptance_evidence: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.status in {"done", "failed", "error", "unverified", "blocked", "skipped"}


class PlanError(ValueError):
    """计划不合规（重复 id、依赖不存在、成环、为空……）。"""


class OrchestrationResult(BaseModel):
    goal: str
    task_id: str = ""
    root_id: str = ""
    attempt_id: str = ""
    status: str = "error"
    error: str = ""
    scheduler: str = "serial"
    duration_ms: float = 0.0
    tokens: int = 0
    run_dir: str = ""
    workspace: str = ""
    subtasks: list[SubTask] = Field(default_factory=list)
    acceptance_report: AcceptanceReport | None = None
    acceptance_spec: AcceptanceSpec | None = None
    acceptance_evidence: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        report = self.acceptance_report
        if self.status != "completed" or report is None or report.verdict != "passed":
            return False
        if (
            self.task_id != self.root_id or report.task_id != self.root_id
            or not self.attempt_id or report.attempt_id != self.attempt_id
        ):
            return False
        try:
            report = AcceptanceReport.model_validate(report)
            spec = AcceptanceSpec.model_validate(self.acceptance_spec)
            validate_report(spec, report)
            validate_evidence(spec, report, self.acceptance_evidence)
        except ValueError:
            return False
        return True

    @property
    def total(self) -> int:
        return len(self.subtasks)

    @property
    def completed(self) -> int:
        return sum(1 for task in self.subtasks if task.status == "done")

    @property
    def completion_rate(self) -> float:
        return self.completed / self.total if self.total else 0.0


# ---------------------------------------------------------------------------
# 规划
# ---------------------------------------------------------------------------
PLANNER_SYSTEM = """你是任务规划器。把用户目标拆成可独立执行、各有明确产物的子任务。

只输出 JSON，不要任何解释或 Markdown 围栏：
{{"subtasks":[
  {{"id":"s1","objective":"做什么（含产物文件名）","assignee":"成员名","deps":[],"acceptance":"怎么算做完"}}
]}}

约束：
1. id 唯一，形如 s1/s2；deps 只能引用其他已存在的 id，且不得成环。
2. 每个子任务必须有一个明确的产物文件（.md 或 .txt）。
3. 子任务数 1..{max_subtasks}；能并行就拆开，不能并行就串成链。
4. assignee 必须从下列成员中选：
{roster}

示例：目标"调研 X 并写报告" → s1 调研产出 sources.md；s2 依赖 s1 写 report.md。
"""

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON（容忍 ```json 围栏与前后废话）。"""
    raw = (text or "").strip()
    fence = _JSON_FENCE.search(raw)
    if fence:
        raw = fence.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise PlanError("输出里找不到 JSON 对象")
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise PlanError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise PlanError("顶层必须是 JSON 对象")
    return payload


def route(assignee: str, objective: str, roster: list[AgentCard], default: str) -> str:
    """把子任务派给一个真实存在的成员。规则优先，避免为路由再烧一次模型。"""
    names = [card.name for card in roster]
    if assignee in names:
        return assignee
    scored = sorted(
        ((card.matches(objective), card.name) for card in roster), reverse=True
    )
    if scored and scored[0][0] > 0:
        return scored[0][1]
    return default if default in names else names[0]


def validate_plan(
    payload: dict[str, Any],
    roster: list[AgentCard],
    max_subtasks: int,
    default_agent: str,
) -> list[SubTask]:
    """严格校验计划。宁可在这里报错，也不要把坏计划带进执行阶段。"""
    items = payload.get("subtasks")
    if not isinstance(items, list) or not items:
        raise PlanError("subtasks 必须是非空数组")
    if len(items) > max_subtasks:
        raise PlanError(f"子任务过多：{len(items)} > {max_subtasks}")

    tasks: list[SubTask] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise PlanError(f"第 {index + 1} 个子任务不是对象")
        task_id = str(item.get("id") or "").strip()
        objective = str(item.get("objective") or "").strip()
        if not task_id:
            raise PlanError(f"第 {index + 1} 个子任务缺少 id")
        if task_id in seen:
            raise PlanError(f"id 重复：{task_id}")
        if not objective:
            raise PlanError(f"{task_id} 缺少 objective")
        seen.add(task_id)

        deps = item.get("deps") or []
        if not isinstance(deps, list):
            raise PlanError(f"{task_id} 的 deps 必须是数组")

        tasks.append(
            SubTask(
                id=task_id,
                objective=objective,
                assignee=route(
                    str(item.get("assignee") or "").strip(), objective, roster, default_agent
                ),
                deps=[str(dep).strip() for dep in deps],
                acceptance=str(item.get("acceptance") or "").strip()
                or "产出非空、包含结论的 Markdown 文件",
            )
        )

    ids = {task.id for task in tasks}
    for task in tasks:
        for dep in task.deps:
            if dep == task.id:
                raise PlanError(f"{task.id} 依赖自己")
            if dep not in ids:
                raise PlanError(f"{task.id} 依赖不存在的 {dep}")

    topological_order(tasks)  # 有环会抛 PlanError
    return tasks


def _kahn(tasks: list[SubTask]) -> list[list[SubTask]]:
    """Kahn 分层：第 k 层只依赖前 k-1 层。同层内按 id 排序，保证串行可复现。"""
    by_id = {task.id: task for task in tasks}
    pending = {task.id: set(task.deps) for task in tasks}
    layers: list[list[SubTask]] = []

    while pending:
        ready = sorted([tid for tid, deps in pending.items() if not deps])
        if not ready:
            raise PlanError(f"依赖成环：{', '.join(sorted(pending))}")
        layers.append([by_id[tid] for tid in ready])
        for tid in ready:
            pending.pop(tid)
        for deps in pending.values():
            deps.difference_update(ready)
    return layers


def topological_layers(tasks: list[SubTask]) -> list[list[SubTask]]:
    return _kahn(tasks)


def topological_order(tasks: list[SubTask]) -> list[SubTask]:
    """摊平成分层，再按层内顺序串起来。确定性来自 id 排序。"""
    return [task for layer in _kahn(tasks) for task in layer]


# ---------------------------------------------------------------------------
# 黑板：执行过程的共享状态（产物索引 + 每步摘要）
# ---------------------------------------------------------------------------
class Board:
    def __init__(self, run_dir: Path | None = None) -> None:
        self.run_dir = run_dir
        self.records: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def record(self, task_id: str, record: Any) -> None:
        """接收 StepRecord。并发模式下会被多个线程调用，必须加锁。"""
        entry = self.records.setdefault(
            task_id, {"steps": 0, "artifacts": [], "error_classes": [], "last_decision": ""}
        )
        with self._lock:
            entry["steps"] += 1
            entry["last_decision"] = getattr(record, "decision_kind", "")
            for obs in getattr(record, "observations", []):
                if obs.artifacts:
                    entry["artifacts"].extend(obs.artifacts)
                if obs.error_class:
                    entry["error_classes"].append(obs.error_class)

    def save(self, tasks: list[SubTask], goal: str) -> Path | None:
        if self.run_dir is None:
            return None
        payload = {
            "goal": goal,
            "subtasks": [task.model_dump(mode="json") for task in tasks],
            "records": self.records,
        }
        path = self.run_dir / "board.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self.records, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 编排器
# ---------------------------------------------------------------------------
class Orchestrator:
    def __init__(
        self,
        *,
        provider: Provider,
        settings: Settings,
        roster: list[AgentCard] | None = None,
        run_dir: Path | str | None = None,
        tracer: Tracer | None = None,
        approve: Callable[[str], bool] | None = None,
        human_handler: Callable[[str], str | None] | None = None,
        acceptances: dict[str, AcceptanceInput] | None = None,
        default_acceptance: AcceptanceInput = None,
        root_acceptance: AcceptanceInput = None,
        agent_factory: Callable[[SubTask], Agent] | None = None,
        bus: MessageBus | None = None,
        enable_delegation: bool = False,
        max_delegation_depth: int = 2,
        max_subtasks: int = 6,
        max_parallel: int = 3,
        default_agent: str = "worker",
        planner_retries: int = 1,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.roster = roster or [
            AgentCard(name="worker", skills=["通用", "整理", "写作"]),
            AgentCard(name="researcher", skills=["调研", "检索", "资料", "搜索"]),
            AgentCard(name="writer", skills=["写作", "文档", "报告", "整理"]),
            AgentCard(name="reviewer", skills=["检查", "评审", "校对"]),
        ]
        self.default_agent = default_agent
        self.run_dir = Path(run_dir) if run_dir is not None else None
        self.max_subtasks = max_subtasks
        self.max_parallel = max_parallel
        self.planner_retries = planner_retries
        self.approve = approve
        self.human_handler = human_handler
        self.acceptances = acceptances or {}
        self.default_acceptance = default_acceptance
        self.root_acceptance = root_acceptance
        self.agent_factory = agent_factory
        self._own_tracer = tracer is None
        self.tracer = tracer or (Tracer(self.run_dir) if self.run_dir else None)
        self._global_budget = Budget.from_limits(settings.limits)
        self._lock = threading.Lock()
        self._subtask_tokens = 0
        self._planner_tokens = 0
        self.bus = bus or (MessageBus(self.run_dir) if self.run_dir else None)
        self.enable_delegation = enable_delegation
        self.max_delegation_depth = max_delegation_depth
        self._board: Board | None = None
        self._ctx = threading.local()  # 记录"当前正在跑哪个子任务、嵌套多深"
        self._root_id: str | None = None
        self._root_attempt_id: str | None = None
        self._workspace: Path | None = None
        self._all_tasks: list[SubTask] = []
        self._registered_ids: set[str] = set()
        self._ended_ids: set[str] = set()
        self._delegate_sequence = 0
        self._tracer_closed = False
        self._run_lock = threading.Lock()

    def _event(self, kind: str, **payload: Any) -> None:
        if self.tracer:
            self.tracer.event(kind, **{
                "schema_version": 2,
                "root_id": self._root_id,
                "root_attempt_id": self._root_attempt_id,
                **payload,
            })

    # ------------------------------------------------------------------ 规划
    def plan(self, goal: str) -> list[SubTask]:
        roster_text = "\n".join(
            f"- {card.name}：{'、'.join(card.skills) or '通用'}" for card in self.roster
        )
        system = PLANNER_SYSTEM.format(max_subtasks=self.max_subtasks, roster=roster_text)
        last_error = ""

        for attempt in range(self.planner_retries + 1):
            messages = [{"role": "system", "content": system}, {"role": "user", "content": goal}]
            if last_error:
                messages.append(
                    {
                        "role": "user",
                        "content": f"上一次输出不合规：{last_error}\n请只输出合法 JSON，不要解释。",
                    }
                )
            response = self.provider.chat(messages, None, temperature=0.0)
            self._planner_tokens += response.usage.total
            self._event(
                "plan_usage", task_id=self._root_id, attempt_id=self._root_attempt_id,
                level="goal", call_id=f"plan-{uuid.uuid4().hex}",
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
            )
            try:
                tasks = validate_plan(
                    extract_json(response.text), self.roster, self.max_subtasks, self.default_agent
                )
            except PlanError as exc:
                last_error = str(exc)
                if self.tracer:
                    self._event(
                        "plan_invalid", goal=goal, attempt=attempt + 1, error=last_error
                    )
                continue

            if self.tracer:
                self._event(
                    "plan",
                    goal=goal,
                    attempts=attempt + 1,
                    subtasks=[
                        {"id": t.id, "assignee": t.assignee, "deps": t.deps, "objective": t.objective}
                        for t in tasks
                    ],
                )
            self._save_plan(tasks, goal, degraded=False)
            return tasks

        # 降级：单任务兜底。宁可跑一个不拆的计划，也不要整个任务失败。
        fallback = [
            SubTask(
                id="s1",
                objective=goal,
                assignee=self.default_agent,
                acceptance="产出非空、包含结论的 Markdown 文件",
            )
        ]
        if self.tracer:
            self._event("plan_degraded", goal=goal, error=last_error)
        self._save_plan(fallback, goal, degraded=True)
        return fallback

    def _save_plan(self, tasks: list[SubTask], goal: str, *, degraded: bool) -> None:
        payload = {
            "goal": goal,
            "degraded": degraded,
            "subtasks": [task.model_dump(mode="json") for task in tasks],
        }
        for directory in {self.run_dir, self._workspace} - {None}:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "plan.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def _register_task(self, task: SubTask, parent_id: str | None = None) -> None:
        """Register logical children before execution, including delegated work."""
        with self._lock:
            if task.id in self._registered_ids:
                raise ValueError(f"duplicate subtask ID: {task.id}")
            task.root_id = self._root_id or ""
            task.task_id = f"{self._root_id}:{task.id}"
            task.parent_id = parent_id or self._root_id
            self._registered_ids.add(task.id)
            self._all_tasks.append(task)
        self._event(
            "task_registered", task_id=task.task_id, subtask=task.id,
            parent_task_id=task.parent_id, level="subtask", assignee=task.assignee,
            objective=task.objective, deps=task.deps,
        )

    def _end_subtask(self, task: SubTask) -> None:
        with self._lock:
            if task.task_id in self._ended_ids:
                return
            self._ended_ids.add(task.task_id)
        self._event(
            "subtask_end", task_id=task.task_id, subtask=task.id,
            attempt_id=task.attempt_id or None, parent_task_id=task.parent_id,
            level="subtask", assignee=task.assignee, status=task.status,
            execution_status=task.execution_status, duration_ms=task.duration_ms,
            steps=task.steps, tool_calls=task.tool_calls, tokens=task.tokens,
            error=task.error, workspace=task.workspace,
            acceptance_spec=task.acceptance_spec.model_dump(mode="json") if task.acceptance_spec else None,
            acceptance_report=task.acceptance_report.model_dump(mode="json") if task.acceptance_report else None,
            acceptance_evidence=task.acceptance_evidence,
        )

    # ------------------------------------------------------------------ 调度
    def run(
        self,
        goal: str,
        scheduler: str = "serial",
        max_parallel: int | None = None,
        task_id: str | None = None,
        goal_id: str | None = None,
    ) -> OrchestrationResult:
        if goal_id is not None and task_id is not None and goal_id != task_id:
            raise ValueError("goal_id and task_id must name the same logical goal")
        root_id = goal_id if goal_id is not None else task_id
        if root_id is not None and (not isinstance(root_id, str) or not root_id.strip()):
            raise ValueError("goal_id/task_id must be a nonblank string")
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("one Orchestrator cannot run concurrent goals")
        try:
            return self._run(goal, scheduler, max_parallel, root_id or f"orch-{uuid.uuid4().hex}")
        finally:
            self._run_lock.release()

    def _run(
        self, goal: str, scheduler: str, max_parallel: int | None, root_id: str,
    ) -> OrchestrationResult:
        started = time.perf_counter()
        self._root_id = root_id
        self._root_attempt_id = f"attempt-{uuid.uuid4().hex}"
        self._all_tasks = []
        self._registered_ids = set()
        self._ended_ids = set()
        self._delegate_sequence = 0
        self._subtask_tokens = self._planner_tokens = 0
        self._global_budget = Budget.from_limits(self.settings.limits)
        self._workspace = None
        self._board = None
        if self._own_tracer and self._tracer_closed and self.run_dir:
            self.tracer = Tracer(self.run_dir)
            self._tracer_closed = False
        identity = {
            "task_id": root_id, "attempt_id": self._root_attempt_id,
            "level": "goal", "parent_task_id": None,
        }
        self._event("case_registered", **identity, goal=goal)
        self._event("orchestration_start", **identity, goal=goal, scheduler=scheduler)
        execution_error = ""
        bound_acceptance = None
        try:
            if self.run_dir:
                self._workspace = self.run_dir / "workspaces" / self._root_attempt_id
                self._workspace.mkdir(parents=True, exist_ok=False)
            else:
                self._workspace = Path(tempfile.mkdtemp(prefix="miniagent-orchestration-"))
            bound_acceptance = bind(
                self.root_acceptance, self._workspace, task_id=root_id, objective=goal,
            )
            board = self._board = Board(self._workspace)
            tasks = self.plan(goal)
            for task in tasks:
                self._register_task(task)
            if self.bus:
                self.bus.post("orchestrator", f"目标：{goal}", kind="system", task_id=root_id)
            if scheduler == "evented":
                self._dispatch_evented(tasks, board, max_parallel or self.max_parallel)
            elif scheduler == "parallel":
                self._dispatch_layered(tasks, board, max_parallel or self.max_parallel)
            elif scheduler == "serial":
                self._dispatch_serial(tasks, board)
            else:
                raise ValueError(f"unknown scheduler: {scheduler}")
        except Exception as exc:
            execution_error = f"{type(exc).__name__}: {exc}"
            for task in self._all_tasks:
                if not task.finished:
                    task.status = "error" if task.status == "running" else "skipped"
                    task.error = f"编排中断：{execution_error}"
                    self._end_subtask(task)

        if self._board is not None:
            try:
                saved = self._board.save(self._all_tasks, goal)
                if self.run_dir is not None and saved is not None:
                    (self.run_dir / "board.json").write_text(saved.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception as exc:
                execution_error = execution_error or f"{type(exc).__name__}: {exc}"
        try:
            evaluation = evaluate(
                bound_acceptance, self._workspace or Path("."),
                task_id=root_id, attempt_id=self._root_attempt_id, objective=goal,
            )
        except Exception as exc:
            execution_error = execution_error or f"{type(exc).__name__}: {exc}"
            evaluation = evaluate(
                None, self._workspace or Path("."), task_id=root_id,
                attempt_id=self._root_attempt_id, objective=goal,
            )
        payload = evaluation.trace_payload()
        self._event("acceptance", **identity, **payload)
        completed = sum(1 for task in self._all_tasks if task.status == "done")
        verdict = evaluation.report.verdict
        if execution_error or verdict == "error":
            status = "error"
        elif verdict == "failed":
            status = "failed_acceptance"
        elif verdict == "unverified":
            status = "unverified"
        else:
            status = "completed" if completed == len(self._all_tasks) else ("partial" if completed else "failed")
        error = execution_error or evaluation.report.reason or ""
        if not error and status != "completed":
            error = f"子任务未全部完成（{completed}/{len(self._all_tasks)}）"
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        result = OrchestrationResult(
            goal=goal, task_id=root_id, root_id=root_id, attempt_id=self._root_attempt_id,
            status=status, error=error,
            scheduler=scheduler,
            duration_ms=duration_ms,
            tokens=self._subtask_tokens + self._planner_tokens,
            run_dir=str(self.run_dir) if self.run_dir else "",
            workspace=str(self._workspace) if self._workspace else "",
            subtasks=self._all_tasks,
            acceptance_spec=evaluation.spec, acceptance_report=evaluation.report,
            acceptance_evidence=payload.get("receipts", []),
        )
        terminal = {
            **identity, "agent": "orchestrator", "status": status,
            "error": error, "execution_error": execution_error,
            "duration_ms": duration_ms, "workspace": result.workspace,
            "steps": sum(task.steps for task in self._all_tasks),
            "tool_calls": sum(task.tool_calls for task in self._all_tasks),
            "acceptance_spec": payload.get("spec"), "acceptance_report": payload.get("report"),
            "acceptance_evidence": result.acceptance_evidence,
        }
        self._event("task_end", **terminal)
        self._event(
            "orchestration_end", **terminal, total=result.total,
            completed=completed, tokens=result.tokens,
        )
        if self.bus:
            self.bus.post(
                "orchestrator",
                f"目标结束：{status}（{completed}/{result.total}）",
                kind="system",
                task_id=root_id,
            )
        self.close()
        return result

    def close(self) -> None:
        """只做了 plan() 没做 run() 时，手动释放自己创建的 tracer。"""
        if self._own_tracer and self.tracer:
            self.tracer.close()
            self._tracer_closed = True

    # -------------------------------------------------------------- S1 串行
    def _dispatch_serial(self, tasks: list[SubTask], board: Board) -> None:
        for task in topological_order(tasks):
            if self._block_if_deps_failed(task, tasks):
                continue
            self._execute(task, board)

    # -------------------------------------------------------------- S2 分层
    def _dispatch_layered(
        self, tasks: list[SubTask], board: Board, max_parallel: int
    ) -> None:
        workers = max(1, max_parallel)
        for layer in topological_layers(tasks):
            runnable: list[SubTask] = []
            for task in layer:
                if self._block_if_deps_failed(task, tasks):
                    continue
                runnable.append(task)
            if not runnable:
                continue
            if workers == 1 or len(runnable) == 1:
                for task in runnable:
                    self._execute(task, board)
                continue
            with ThreadPoolExecutor(max_workers=min(workers, len(runnable))) as pool:
                futures = [pool.submit(self._execute, task, board) for task in runnable]
                for future in futures:
                    future.result()  # _execute 内部已兜底异常

    # ------------------------------------------------------------------ 执行
    def _block_if_deps_failed(self, task: SubTask, tasks: list[SubTask]) -> bool:
        by_id = {item.id: item for item in tasks}
        broken = [
            dep for dep in task.deps if by_id[dep].status != "done"
        ]
        if not broken:
            return False
        task.status = "blocked"
        task.error = f"上游未完成：{', '.join(broken)}"
        self._end_subtask(task)
        return True

    def _execute(self, task: SubTask, board: Board, depth: int = 0) -> None:
        if task.status != "pending":
            return
        if self._global_exhausted():
            task.status = "skipped"
            task.error = "全局预算耗尽"
            self._end_subtask(task)
            return

        # 记录本任务在"委派树"里的位置；委派出去的子任务会看到 depth+1
        previous = (getattr(self._ctx, "task", None), getattr(self._ctx, "depth", 0))
        self._ctx.task = task
        self._ctx.depth = depth

        task.status = "running"
        task.attempt_id = f"attempt-{uuid.uuid4().hex}"
        started = time.perf_counter()
        self._event(
            "subtask_start", task_id=task.task_id, attempt_id=task.attempt_id,
            parent_task_id=task.parent_id, level="subtask", subtask=task.id,
            assignee=task.assignee, deps=task.deps, objective=task.objective,
        )
        if self.bus:
            self.bus.post(
                "orchestrator",
                f"开始 {task.id} → {task.assignee}",
                kind="system",
                task_id=task.task_id,
                recipients=[task.assignee],
            )

        try:
            agent = self.agent_factory(task) if self.agent_factory else self._build_agent(task)
            result: RunResult = agent.run(
                task.objective,
                task_id=task.task_id,
                root_id=task.root_id,
                parent_task_id=task.parent_id,
                attempt_id=task.attempt_id,
                acceptance=self._acceptance_for(task),
                budget=Budget.from_limits(self.settings.limits, factor=0.8),
                on_step=lambda record, _state: board.record(task.id, record),
            )
            if result.task_id != task.task_id or result.attempt_id != task.attempt_id:
                raise ValueError("agent result task/attempt identity does not match dispatched work")
            task.execution_status = result.status
            if result.succeeded:
                task.status = "done"
            elif result.status == "unverified" or result.status == "completed":
                task.status = "unverified"
            elif result.status == "error":
                task.status = "error"
            else:
                task.status = "failed"
            task.result = result.answer
            task.error = result.error
            task.steps = result.steps
            task.tool_calls = result.tool_calls
            task.tokens = result.usage.total
            task.workspace = result.sandbox
            task.acceptance_spec = result.acceptance_spec
            task.acceptance_report = result.acceptance_report
            task.acceptance_evidence = result.acceptance_evidence
        except Exception as exc:  # 单个子任务炸掉不能拖垮整张图
            task.status = "error"
            task.execution_status = "error"
            task.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._ctx.task, self._ctx.depth = previous

        task.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        with self._lock:
            self._subtask_tokens += task.tokens
            self._global_budget.spend(tokens=task.tokens, seconds=task.duration_ms / 1000)

        self._end_subtask(task)
        if self.bus:
            self.bus.post(
                task.assignee,
                f"{task.id} {task.status}",
                kind="system",
                task_id=task.task_id,
                status=task.status,
            )
            if task.result:
                # 每个 agent 的最终答复都会进群聊频道 —— 这就是最朴素的"多 agent 群聊"
                self.bus.post(task.assignee, task.result, kind="report", task_id=task.task_id)

    # ------------------------------------------------------------------ 组装
    def _build_agent(self, task: SubTask) -> Agent:
        if self._workspace is None:
            raise RuntimeError("child agents require an active orchestration workspace")
        root = self._workspace / "agents" / ("task-" + quote(task.id, safe=""))
        task.workspace = str(root)
        return Agent(
            name=task.assignee,
            provider=self.provider,
            registry=build_default_registry(),
            sandbox=Sandbox(root, agent_name=task.assignee),
            settings=self.settings,
            tracer=self.tracer,
            approve=self.approve,
            human_handler=self.human_handler,
            delegate_handler=(self._handle_delegate if self.enable_delegation else None),
        )

    # -------------------------------------------------------------- S3 事件驱动
    def _dispatch_evented(
        self, tasks: list[SubTask], board: Board, max_parallel: int
    ) -> None:
        """依赖驱动：谁的依赖一完成，谁立刻可跑（不等整层）。

        与 S2 的差别：S2 是"层栅栏"，同层最慢的任务决定下一层何时开始；
        S3 按完成事件解锁，能省掉与关键路径无关的等待。代价是调度逻辑更绕、
        并发错误更难复现（所以要有 S1 当对照组）。
        """
        workers = max(1, max_parallel)
        dependents: dict[str, list[SubTask]] = {task.id: [] for task in tasks}
        remaining: dict[str, set[str]] = {}
        for task in tasks:
            remaining[task.id] = set(task.deps)
            for dep in task.deps:
                dependents[dep].append(task)

        ready = sorted([task for task in tasks if not task.deps], key=lambda t: t.id)
        inflight: dict[Any, SubTask] = {}
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            while ready or inflight:
                while ready and len(inflight) < workers:
                    task = ready.pop(0)
                    if task.status != "pending":
                        continue
                    if self._global_exhausted():
                        task.status, task.error = "skipped", "全局预算耗尽"
                        self._end_subtask(task)
                        self._propagate_block(task, dependents)
                        continue
                    inflight[pool.submit(self._execute, task, board)] = task

                if not inflight:
                    break

                done, _pending = wait(list(inflight), return_when=FIRST_COMPLETED)
                for future in done:
                    task = inflight.pop(future)
                    future.result()  # _execute 内部已兜底异常
                    if task.status != "done":
                        self._propagate_block(task, dependents)
                        continue
                    for child in dependents[task.id]:
                        remaining[child.id].discard(task.id)
                        if not remaining[child.id] and child.status == "pending":
                            ready.append(child)
                    ready.sort(key=lambda item: item.id)
        finally:
            pool.shutdown(wait=True)

    def _propagate_block(
        self, task: SubTask, dependents: dict[str, list[SubTask]]
    ) -> None:
        """一个任务没完成 → 它所有（传递）下游立刻标 blocked，避免调度器空转。"""
        queue = list(dependents.get(task.id, []))
        while queue:
            child = queue.pop()
            if child.status != "pending":
                continue
            child.status = "blocked"
            child.error = f"上游 {task.id} 未完成"
            self._end_subtask(child)
            if self.bus:
                self.bus.post(
                    "orchestrator",
                    f"{child.id} 被阻塞（上游 {task.id}）",
                    kind="system",
                    task_id=child.task_id,
                    recipients=[child.assignee],
                )
            queue.extend(dependents.get(child.id, []))

    # ------------------------------------------------------------------ 委派
    def _handle_delegate(self, payload: dict[str, Any]) -> Observation:
        """RAO 的 ``delegate_task`` 落到这里：真的去跑一个子任务（递归、限深）。"""
        parent_task = getattr(self._ctx, "task", None)
        parent = parent_task.task_id if parent_task is not None else "agent"
        depth = int(getattr(self._ctx, "depth", 0) or 0)
        step_id = f"{parent}:delegate"
        objective = str(payload.get("objective") or "").strip()

        if not objective:
            return Observation(
                step_id=step_id,
                source="delegate",
                ok=False,
                error_class=ErrorClass.A_INVALID_ARGS,
                content="delegate_task 缺少 objective，无法委派。",
            )
        if not self.enable_delegation or self._board is None or parent_task is None:
            return Observation(
                step_id=step_id,
                source="delegate",
                ok=False,
                error_class=ErrorClass.A_UNSUPPORTED,
                content="当前未启用委派，请自己完成该子任务。",
            )
        if depth >= self.max_delegation_depth:
            return Observation(
                step_id=step_id,
                source="delegate",
                ok=False,
                error_class=ErrorClass.A_UNSUPPORTED,
                content=f"委派深度已达上限（{self.max_delegation_depth}），请自行完成。",
            )

        with self._lock:
            while True:
                self._delegate_sequence += 1
                child_id = f"{parent_task.id}.d{depth + 1}.{self._delegate_sequence}"
                if child_id not in self._registered_ids:
                    break
        child = SubTask(
            id=child_id,
            objective=objective,
            assignee=route(
                str(payload.get("assignee") or ""), objective, self.roster, self.default_agent
            ),
            acceptance=str(payload.get("acceptance") or "").strip()
            or "产出非空、包含结论的 Markdown 文件",
        )
        self._register_task(child, parent_id=parent)
        if self.bus:
            self.bus.post(
                parent,
                objective,
                kind="delegate_request",
                task_id=child.task_id,
                recipients=[child.assignee],
            )
        self._execute(child, self._board, depth + 1)
        if self.bus:
            self.bus.post(
                child.assignee,
                f"{child.id} {child.status}",
                kind="delegate_result",
                task_id=child.task_id,
                recipients=[parent],
            )
        ok = child.status == "done"
        return Observation(
            step_id=step_id,
            source="delegate",
            ok=ok,
            content=(child.result or child.error or f"子任务 {child.id} 结束（{child.status}）")[:4000],
            error_class=None if ok else ErrorClass.A_TOOL_ERROR,
        )

    def _acceptance_for(self, task: SubTask) -> AcceptanceInput:
        return self.acceptances.get(task.id, self.default_acceptance)

    def _global_exhausted(self) -> bool:
        return self._global_budget.exhausted or self._global_budget.tokens_left <= 0


__all__ = [
    "AgentCard",
    "Board",
    "OrchestrationResult",
    "Orchestrator",
    "PlanError",
    "SubTask",
    "extract_json",
    "route",
    "topological_layers",
    "topological_order",
    "validate_plan",
]
