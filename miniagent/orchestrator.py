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
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from .agent import Agent
from .bus import MessageBus
from .config import Settings
from .metrics import Tracer
from .providers import Provider
from .rao import Acceptance, Budget, ErrorClass, Observation
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
    parent_id: str | None = None
    objective: str
    assignee: str = "worker"
    deps: list[str] = Field(default_factory=list)
    acceptance: str = ""
    """自然语言验收标准（给人看、也喂给执行者）。代码级验收由 acceptances 注入。"""

    status: str = "pending"
    """pending / running / done / failed / blocked / skipped"""

    result: str = ""
    error: str = ""
    steps: int = 0
    tool_calls: int = 0
    duration_ms: float = 0.0
    tokens: int = 0

    @property
    def finished(self) -> bool:
        return self.status in {"done", "failed", "blocked", "skipped"}


class PlanError(ValueError):
    """计划不合规（重复 id、依赖不存在、成环、为空……）。"""


class OrchestrationResult(BaseModel):
    goal: str
    status: str = "failed"  # completed / partial / failed
    scheduler: str = "serial"
    duration_ms: float = 0.0
    tokens: int = 0
    run_dir: str = ""
    subtasks: list[SubTask] = Field(default_factory=list)

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
            "subtasks": [task.model_dump() for task in tasks],
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
        acceptances: dict[str, Acceptance] | None = None,
        default_acceptance: Acceptance | None = None,
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
        self.agent_factory = agent_factory
        self._own_tracer = tracer is None
        self.tracer = tracer or (Tracer(self.run_dir) if self.run_dir else None)
        self._global_budget = Budget.from_limits(settings.limits)
        self._lock = threading.Lock()
        self._subtask_tokens = 0
        self.bus = bus or (MessageBus(self.run_dir) if self.run_dir else None)
        self.enable_delegation = enable_delegation
        self.max_delegation_depth = max_delegation_depth
        self._board: Board | None = None
        self._ctx = threading.local()  # 记录"当前正在跑哪个子任务、嵌套多深"

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
            try:
                tasks = validate_plan(
                    extract_json(response.text), self.roster, self.max_subtasks, self.default_agent
                )
            except PlanError as exc:
                last_error = str(exc)
                if self.tracer:
                    self.tracer.event(
                        "plan_invalid", goal=goal, attempt=attempt + 1, error=last_error
                    )
                continue

            if self.tracer:
                self.tracer.event(
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
            self.tracer.event("plan_degraded", goal=goal, error=last_error)
        self._save_plan(fallback, goal, degraded=True)
        return fallback

    def _save_plan(self, tasks: list[SubTask], goal: str, *, degraded: bool) -> None:
        if self.run_dir is None:
            return
        payload = {
            "goal": goal,
            "degraded": degraded,
            "subtasks": [task.model_dump() for task in tasks],
        }
        (self.run_dir / "plan.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ------------------------------------------------------------------ 调度
    def run(
        self,
        goal: str,
        scheduler: str = "serial",
        max_parallel: int | None = None,
        task_id: str | None = None,
    ) -> OrchestrationResult:
        task_id = task_id or f"orch-{uuid.uuid4().hex[:8]}"
        started = time.perf_counter()
        board = Board(self.run_dir)

        if self.tracer:
            self.tracer.event("orchestration_start", goal=goal, task_id=task_id, scheduler=scheduler)

        tasks = self.plan(goal)
        self._board = board
        if self.bus:
            self.bus.post("orchestrator", f"目标：{goal}", kind="system", task_id=task_id)
        if scheduler == "evented":
            self._dispatch_evented(tasks, board, max_parallel or self.max_parallel)
        elif scheduler == "parallel":
            self._dispatch_layered(tasks, board, max_parallel or self.max_parallel)
        else:
            self._dispatch_serial(tasks, board)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        completed = sum(1 for task in tasks if task.status == "done")
        status = "completed" if completed == len(tasks) else ("partial" if completed else "failed")
        result = OrchestrationResult(
            goal=goal,
            status=status,
            scheduler=scheduler,
            duration_ms=duration_ms,
            tokens=self._subtask_tokens,
            run_dir=str(self.run_dir) if self.run_dir else "",
            subtasks=tasks,
        )

        board.save(tasks, goal)
        if self.tracer:
            # 顶层记一条 task_end，让 metrics 的"总体目标完成率"也能算出来；
            # 子任务层用 subtask_end 单独统计（课程要求两者都报）。
            self.tracer.event(
                "task_end",
                task_id=task_id,
                agent="orchestrator",
                level="goal",  # 与子任务级 task_end 区分：指标里两者不能混算
                status="completed" if status == "completed" else "failed",
                duration_ms=duration_ms,
                steps=sum(task.steps for task in tasks),
                tool_calls=sum(task.tool_calls for task in tasks),
                prompt_tokens=0,
                completion_tokens=self._subtask_tokens,
                error="" if status == "completed" else f"子任务未全部完成（{completed}/{len(tasks)}）",
            )
            self.tracer.event(
                "orchestration_end",
                task_id=task_id,
                status=status,
                duration_ms=duration_ms,
                total=len(tasks),
                completed=completed,
                tokens=self._subtask_tokens,
            )
        if self.bus:
            self.bus.post(
                "orchestrator",
                f"目标结束：{status}（{completed}/{len(tasks)}）",
                kind="system",
                task_id=task_id,
            )
        if self._own_tracer and self.tracer:
            self.tracer.close()
        return result

    def close(self) -> None:
        """只做了 plan() 没做 run() 时，手动释放自己创建的 tracer。"""
        if self._own_tracer and self.tracer:
            self.tracer.close()

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
            dep for dep in task.deps if by_id[dep].status in {"failed", "blocked", "skipped"}
        ]
        if not broken:
            return False
        task.status = "blocked"
        task.error = f"上游未完成：{', '.join(broken)}"
        if self.tracer:
            self.tracer.event(
                "subtask_end", subtask=task.id, status="blocked", error=task.error
            )
        return True

    def _execute(self, task: SubTask, board: Board, depth: int = 0) -> None:
        if task.status != "pending":
            return
        if self._global_exhausted():
            task.status = "skipped"
            task.error = "全局预算耗尽"
            if self.tracer:
                self.tracer.event("subtask_end", subtask=task.id, status="skipped", error=task.error)
            return

        # 记录本任务在"委派树"里的位置；委派出去的子任务会看到 depth+1
        previous = (getattr(self._ctx, "task_id", None), getattr(self._ctx, "depth", 0))
        self._ctx.task_id = task.id
        self._ctx.depth = depth

        task.status = "running"
        started = time.perf_counter()
        if self.tracer:
            self.tracer.event(
                "subtask_start",
                subtask=task.id,
                assignee=task.assignee,
                deps=task.deps,
                objective=task.objective,
            )
        if self.bus:
            self.bus.post(
                "orchestrator",
                f"开始 {task.id} → {task.assignee}",
                kind="system",
                task_id=task.id,
                recipients=[task.assignee],
            )

        try:
            agent = self.agent_factory(task) if self.agent_factory else self._build_agent(task)
            result: RunResult = agent.run(
                task.objective,
                task_id=task.id,
                acceptance=self._acceptance_for(task),
                budget=Budget.from_limits(self.settings.limits, factor=0.8),
                on_step=lambda record, _state: board.record(task.id, record),
            )
            task.status = "done" if result.status == "completed" else "failed"
            task.result = result.answer
            task.error = result.error
            task.steps = result.steps
            task.tool_calls = result.tool_calls
            task.tokens = result.usage.total
        except Exception as exc:  # 单个子任务炸掉不能拖垮整张图
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"

        task.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        with self._lock:
            self._subtask_tokens += task.tokens
            self._global_budget.spend(tokens=task.tokens, seconds=task.duration_ms / 1000)

        if self.tracer:
            self.tracer.event(
                "subtask_end",
                subtask=task.id,
                assignee=task.assignee,
                status=task.status,
                duration_ms=task.duration_ms,
                steps=task.steps,
                tokens=task.tokens,
                error=task.error,
            )
        if self.bus:
            self.bus.post(
                task.assignee,
                f"{task.id} {task.status}",
                kind="system",
                task_id=task.id,
                status=task.status,
            )
            if task.result:
                # 每个 agent 的最终答复都会进群聊频道 —— 这就是最朴素的"多 agent 群聊"
                self.bus.post(task.assignee, task.result, kind="report", task_id=task.id)

        self._ctx.task_id, self._ctx.depth = previous

    # ------------------------------------------------------------------ 组装
    def _build_agent(self, task: SubTask) -> Agent:
        root = (self.run_dir or Path("runs")) / "agents" / f"{task.assignee}-{task.id}"
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
                        if self.tracer:
                            self.tracer.event(
                                "subtask_end", subtask=task.id, status="skipped", error=task.error
                            )
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
            if self.tracer:
                self.tracer.event(
                    "subtask_end",
                    subtask=child.id,
                    assignee=child.assignee,
                    status="blocked",
                    error=child.error,
                )
            if self.bus:
                self.bus.post(
                    "orchestrator",
                    f"{child.id} 被阻塞（上游 {task.id}）",
                    kind="system",
                    task_id=child.id,
                    recipients=[child.assignee],
                )
            queue.extend(dependents.get(child.id, []))

    # ------------------------------------------------------------------ 委派
    def _handle_delegate(self, payload: dict[str, Any]) -> Observation:
        """RAO 的 ``delegate_task`` 落到这里：真的去跑一个子任务（递归、限深）。"""
        parent = str(getattr(self._ctx, "task_id", "") or "agent")
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
        if not self.enable_delegation or self._board is None:
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

        child = SubTask(
            id=f"{parent}.d{depth + 1}",
            objective=objective,
            assignee=route(
                str(payload.get("assignee") or ""), objective, self.roster, self.default_agent
            ),
            acceptance=str(payload.get("acceptance") or "").strip()
            or "产出非空、包含结论的 Markdown 文件",
        )
        if self.bus:
            self.bus.post(
                parent,
                objective,
                kind="delegate_request",
                task_id=child.id,
                recipients=[child.assignee],
            )
        self._execute(child, self._board, depth + 1)
        if self.bus:
            self.bus.post(
                child.assignee,
                f"{child.id} {child.status}",
                kind="delegate_result",
                task_id=child.id,
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

    def _acceptance_for(self, task: SubTask) -> Acceptance | None:
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
