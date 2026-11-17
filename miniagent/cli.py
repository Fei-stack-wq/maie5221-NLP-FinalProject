"""命令行入口。

用法（在仓库根目录）：
    python -m miniagent demo                       # 离线演示，不需要 API key
    python -m miniagent run "写一份关于 X 的笔记"     # 真实调用 DeepSeek
    python -m miniagent eval --tasks examples/tasks.jsonl
    python -m miniagent report runs/<run_id>

设计原则：UI 只是"触发一次 run + 看结果"。课程明确说复杂前端不加分，
所以这里只做到"能跑、能看、能出报告"。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .agent import Agent
from .config import Settings
from .metrics import Tracer, load_events, render_report, summarize
from .providers import DeepSeekProvider, Provider, ScriptedProvider
from .acceptance import AcceptanceInput, accept_files
from .orchestrator import Orchestrator
from .sandbox import Sandbox
from .schemas import RunResult
from .tools import build_default_registry

# 离线演示剧本：完整走一遍「列目录 -> 写文件 -> 读文件 -> 执行代码 -> 最终答复」。
# 它同时是一份"模型该长什么样"的说明书，写 prompt 时可以对照。
DEMO_SCRIPT: list[dict[str, Any]] = [
    {"tool_calls": [{"name": "list_dir", "arguments": {"path": "."}}]},
    {
        "tool_calls": [
            {
                "name": "write_file",
                "arguments": {
                    "path": "notes.md",
                    "content": (
                        "# 演示产物\n\n"
                        "这个文件是 `ScriptedProvider` 驱动 agent 循环写出来的，全程没有联网、没有 API key。\n\n"
                        "## 它证明了什么\n"
                        "- 工具调用闭环（模型给参数 -> 代码执行 -> 结果回传）\n"
                        "- 沙箱路径限制（写不到工作区之外）\n"
                        "- 指标采集（每个 step 都有 token 与耗时）\n"
                    ),
                },
            }
        ]
    },
    {"tool_calls": [{"name": "read_file", "arguments": {"path": "notes.md"}}]},
    {"tool_calls": [{"name": "run_python", "arguments": {"code": "print(2 ** 10)"}}]},
    {
        "text": (
            "## 完成\n\n"
            "- 生成 `notes.md`\n"
            "- 回读验证成功\n"
            "- 沙箱内 Python 可执行（2**10 = 1024）\n\n"
            "详见同目录下的 `trace.jsonl` 与 `report.md`。"
        )
    },
]


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    _enable_utf8()
    settings = Settings.from_env()

    parser = argparse.ArgumentParser(
        prog="miniagent",
        description="多智能体协作系统：RAO 原子 + 编排调度 + 消息总线 + 指标",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="离线演示（ScriptedProvider，无需 API key）")
    demo.add_argument("--runs-dir", default=str(settings.runs_dir))
    demo.add_argument("--agent", default="demo-agent")
    demo.add_argument("--yes", action="store_true", help="自动批准危险操作（无人值守）")

    run = sub.add_parser("run", help="运行一个任务（默认真实调用 DeepSeek）")
    run.add_argument("task", help="自然语言任务描述")
    run.add_argument("--agent", default="worker")
    run.add_argument("--mock", metavar="SCRIPT.json", help="用剧本文件代替真实模型")
    run.add_argument("--runs-dir", default=str(settings.runs_dir))
    run.add_argument("--yes", action="store_true", help="自动批准危险操作（无人值守）")
    run.add_argument("--acceptance", metavar="CHECKS.json", help="由用户提供的文件验收规则")

    evaluate = sub.add_parser("eval", help="批量评测：输出完成率与耗时")
    evaluate.add_argument("--tasks", default="examples/tasks.jsonl", help="任务集 JSONL")
    evaluate.add_argument("--mock", metavar="SCRIPT.json", help="用剧本文件代替真实模型")
    evaluate.add_argument("--runs-dir", default=str(settings.runs_dir))
    evaluate.add_argument("--agent", default="worker")
    evaluate.add_argument("--yes", action="store_true")
    evaluate.add_argument("--acceptance", metavar="CHECKS.json", help="默认文件验收规则；逐题 acceptance 字段优先")

    orchestrate = sub.add_parser(
        "orchestrate", help="把一句目标拆成子任务图并调度执行（S1 串行 / S2 分层并发 / S3 事件驱动）"
    )
    orchestrate.add_argument("goal", help="自然语言目标")
    orchestrate.add_argument(
        "--scheduler", choices=["serial", "parallel", "evented"], default="serial",
        help="serial=一次一个（默认、可复现）；parallel=按层并发；evented=依赖完成即解锁",
    )
    orchestrate.add_argument("--max-parallel", type=int, default=3, help="并发上限")
    orchestrate.add_argument(
        "--delegate", action="store_true",
        help="允许 agent 用 delegate_task 把子任务再委派出去（深度默认 2）",
    )
    orchestrate.add_argument(
        "--mock", metavar="SCRIPT.json",
        help="剧本文件（按 plan→子任务 顺序排列；仅 serial 可用）",
    )
    orchestrate.add_argument("--runs-dir", default=str(settings.runs_dir))
    orchestrate.add_argument("--yes", action="store_true", help="自动批准危险操作（无人值守）")
    orchestrate.add_argument("--acceptance", metavar="CHECKS.json", help="独立 root 与各子任务的文件验收规则")

    report = sub.add_parser("report", help="汇总一个 run 目录，生成 report.md")
    report.add_argument("run_dir")

    args = parser.parse_args(argv)

    if args.command == "demo":
        return _cmd_demo(args, settings)
    if args.command == "run":
        return _cmd_run(args, settings)
    if args.command == "eval":
        return _cmd_eval(args, settings)
    if args.command == "orchestrate":
        return _cmd_orchestrate(args, settings)
    if args.command == "report":
        return _cmd_report(args)
    parser.print_help()
    return 1


# ---------------------------------------------------------------------------
def _cmd_demo(args: argparse.Namespace, settings: Settings) -> int:
    run_dir = _new_run_dir(Path(args.runs_dir))
    return _execute(
        tasks=[{"task_id": "demo-1", "task": "演示一次完整的 agent 工具调用闭环。"}],
        provider_factory=lambda: ScriptedProvider(DEMO_SCRIPT),
        run_dir=run_dir,
        settings=settings,
        agent_name=args.agent,
        auto_approve=args.yes,
        # 演示验收：只要 notes.md 存在且非空就算完成。
        # 把 "completed" 从"模型说完了"改成"产物真的在"，这是指标可信的前提。
        acceptance=accept_files("notes.md"),
    )


def _cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    run_dir = _new_run_dir(Path(args.runs_dir))
    try:
        acceptance = _load_acceptance(args.acceptance)
        provider_factory = _make_provider_factory(args.mock, settings)
    except Exception as exc:  # 没配 key 时给出可操作的提示，而不是堆栈
        print(f"[配置错误] {exc}", file=sys.stderr)
        _record_batch_setup_error([{"task_id": "run-1", "task": args.task}], run_dir, exc)
        return 2
    return _execute(
        tasks=[{"task_id": "run-1", "task": args.task}],
        provider_factory=provider_factory,
        run_dir=run_dir,
        settings=settings,
        agent_name=args.agent,
        auto_approve=args.yes,
        acceptance=acceptance,
    )


def _cmd_eval(args: argparse.Namespace, settings: Settings) -> int:
    try:
        tasks = _load_tasks(Path(args.tasks))
    except (ValueError, OSError) as exc:
        print(f"[任务集错误] {exc}", file=sys.stderr)
        return 2
    if not tasks:
        print(f"[错误] 任务集为空或不存在：{args.tasks}", file=sys.stderr)
        return 2
    run_dir = _new_run_dir(Path(args.runs_dir))
    try:
        acceptance = _load_acceptance(args.acceptance)
        provider_factory = _make_provider_factory(args.mock, settings)
    except Exception as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        _record_batch_setup_error(tasks, run_dir, exc)
        return 2
    return _execute(
        tasks=tasks,
        provider_factory=provider_factory,
        run_dir=run_dir,
        settings=settings,
        agent_name=args.agent,
        auto_approve=args.yes,
        acceptance=acceptance,
    )


def _cmd_orchestrate(args: argparse.Namespace, settings: Settings) -> int:
    # 先校验再建目录：被拒绝的命令不该在工作区留下空 run 目录
    if args.mock and args.scheduler != "serial":
        print(
            "[错误] --mock 用的 ScriptedProvider 带游标、不是线程安全的；"
            "并行/事件驱动模式请用真实模型，或注入自定义 provider。",
            file=sys.stderr,
        )
        return 2
    run_dir = _new_run_dir(Path(args.runs_dir))
    goal_id = f"orch-{uuid.uuid4().hex}"
    try:
        root_acceptance, acceptances = _load_orchestration_acceptance(args.acceptance)
        provider = _make_provider_factory(args.mock, settings)()
    except Exception as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        _record_batch_setup_error([{"task_id": goal_id, "task": args.goal}], run_dir, exc)
        return 2

    tracer = Tracer(run_dir)
    orchestrator = Orchestrator(
        provider=provider,
        settings=settings,
        run_dir=run_dir,
        tracer=tracer,
        root_acceptance=root_acceptance,
        acceptances=acceptances,
        enable_delegation=args.delegate,
        approve=(lambda _prompt: True) if args.yes else None,
        human_handler=(
            (lambda _q: "（无人值守模式）请基于已有信息自行判断继续，不要再次询问。")
            if args.yes
            else None
        ),
    )
    try:
        result = orchestrator.run(
            args.goal, scheduler=args.scheduler, max_parallel=args.max_parallel, goal_id=goal_id
        )
    finally:
        tracer.close()

    print(f"\n目标：{result.goal}")
    print(
        f"调度：{result.scheduler} | 状态：{result.status}"
        f" | 总耗时 {result.duration_ms / 1000:.2f}s | token {result.tokens}"
    )
    print("子任务：")
    for task in result.subtasks:
        mark = {"done": "✅", "blocked": "⏭", "skipped": "⏭"}.get(task.status, "❌")
        suffix = f" | {task.error}" if task.error else ""
        print(
            f"  {mark} {task.id} [{task.assignee}] {task.status}"
            f" | {task.duration_ms / 1000:.2f}s | {task.steps} 步"
            f" | {task.tokens} token{suffix}"
        )
    print(f"子任务完成率：{result.completion_rate:.1%} ({result.completed}/{result.total})")

    if orchestrator.bus and orchestrator.bus.count:
        print("\n群聊频道（bus.jsonl，最近 10 条）：")
        print(orchestrator.bus.transcript(limit=10))

    print("\n" + render_report(run_dir))
    print(f"运行目录：{run_dir.resolve()}")
    return 0 if result.succeeded else 1


def _cmd_report(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    if not (run_dir / "trace.jsonl").exists():
        print(f"[错误] 找不到 trace：{run_dir / 'trace.jsonl'}", file=sys.stderr)
        return 2
    print(render_report(run_dir))
    return 0


# ---------------------------------------------------------------------------
def _execute(
    tasks: list[dict[str, Any]],
    provider_factory: Callable[[], Provider],
    run_dir: Path,
    settings: Settings,
    agent_name: str,
    auto_approve: bool,
    acceptance: AcceptanceInput = None,
) -> int:
    """跑一批任务，然后打印指标。批量跑是"完成率"这个指标的唯一来源。

    ``provider_factory`` 而不是 provider 实例：每个任务都必须是干净的状态。
    之前用同一个 ScriptedProvider 跑 3 个任务，后两个直接"剧本耗尽"，
    却被算成 completed —— 指标虚高，是最危险的一类 bug。
    """
    results = []
    with Tracer(run_dir) as tracer:
        for index, item in enumerate(tasks, start=1):
            case_id = item.get("task_id") or f"task-{index}"
            tracer.event(
                "case_registered", schema_version=2, root_id=case_id,
                task_id=case_id, level="goal", task=item["task"],
            )
        for index, item in enumerate(tasks, start=1):
            task_id = item.get("task_id") or f"task-{index}"
            print(f"\n=== [{index}/{len(tasks)}] {task_id} ===")
            print(f"任务：{item['task'][:120]}")
            try:
                selected = _file_acceptance(item["acceptance"]) if "acceptance" in item else acceptance
                provider = provider_factory()
                # Directory identity is independent of caller-controlled task IDs.
                directory = f"{quote(agent_name, safe='')}-{uuid.uuid4().hex}"
                sandbox = Sandbox(run_dir / "agents" / directory, agent_name=agent_name)
                agent = Agent(
                    name=agent_name, provider=provider,
                    registry=build_default_registry(), sandbox=sandbox,
                    settings=settings, tracer=tracer,
                    approve=(lambda _prompt: True) if auto_approve else None,
                    human_handler=(
                        (lambda _q: "（无人值守模式）请基于已有信息自行判断继续，不要再次询问。")
                        if auto_approve else None
                    ),
                )
                result = agent.run(item["task"], task_id=task_id, acceptance=selected)
            except Exception as exc:
                result = RunResult(
                    task_id=task_id, attempt_id=f"setup-{uuid.uuid4().hex}",
                    agent=agent_name, status="error",
                    error=f"任务初始化失败：{type(exc).__name__}: {exc}",
                )
                tracer.event(
                    "task_end", schema_version=2, level="goal", root_id=task_id,
                    **result.model_dump(mode="json"),
                )
            results.append(result)

            status_text = "✅ completed" if result.succeeded else f"❌ {result.status}"
            print(
                f"结果：{status_text}"
                f" | 步数 {result.steps}"
                f" | 工具 {result.tool_calls}"
                f" | 耗时 {result.duration_ms / 1000:.2f}s"
                f" | token {result.usage.total}"
            )
            if result.error:
                print(f"原因：{result.error}")
            if result.answer:
                preview = result.answer.strip().splitlines()[:6]
                print("答复：" + "\n      ".join(preview))
            print(f"沙箱：{result.sandbox}")

    summary = summarize(load_events(run_dir))
    print("\n" + "=" * 60)
    print("汇总指标")
    print("=" * 60)
    print(f"任务完成率 : {summary['completion_rate']:.1%}  ({summary['completed']}/{summary['total_tasks']})")
    print(f"平均耗时   : {summary['mean_duration_ms'] / 1000:.2f}s   P95 {summary['p95_duration_ms'] / 1000:.2f}s")
    print(f"平均步数   : {summary['mean_steps']}")
    print(f"总 token   : {summary['total_tokens']}")
    if summary["failure_modes"]:
        print(f"失败模式   : {summary['failure_modes']}")

    # 落盘：报告里的数字必须与上面打印的完全一致（同一个 summarize 函数）
    render_report(run_dir)

    print(f"\n运行目录   : {run_dir.resolve()}")
    print(f"报告       : {(run_dir / 'report.md').resolve()}")

    ok = all(result.succeeded for result in results)
    return 0 if ok else 1


def _make_provider_factory(
    mock_path: str | None, settings: Settings
) -> Callable[[], Provider]:
    """返回一个"每次调用都产出一个干净 provider"的工厂。

    真实 provider 是无状态的，复用同一个实例没问题；
    ScriptedProvider 有游标，必须每条任务重新读一遍剧本。
    """
    if mock_path:
        path = Path(mock_path)
        ScriptedProvider.from_json(path)  # 先解析一次：剧本格式错误要在这里暴露
        return lambda: ScriptedProvider.from_json(path)

    provider = DeepSeekProvider(
        api_key=settings.api_key,
        base_url=settings.base_url,
        model=settings.model,
    )
    return lambda: provider


def _new_run_dir(base: Path) -> Path:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    path = base / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file_acceptance(payload: Any) -> AcceptanceInput:
    """Parse host-supplied deterministic checks; model plans never call this."""
    if payload is None:
        return None
    if not isinstance(payload, dict) or set(payload) - {"files", "min_bytes", "min_lines"}:
        raise ValueError("验收须为 files/min_bytes/min_lines 对象")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("验收 files 必须是非空数组")
    names, contains, hashes = [], {}, {}
    for item in files:
        if not isinstance(item, dict) or set(item) - {"path", "contains", "sha256"}:
            raise ValueError("每个 files 项只接受 path/contains/sha256")
        name = item.get("path")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("验收文件 path 必须非空且不可重复")
        names.append(name)
        for key, values in (("contains", contains), ("sha256", hashes)):
            if key in item:
                if not isinstance(item[key], str) or not item[key]:
                    raise ValueError(f"{key} 必须是非空字符串")
                values[name] = item[key]
    options = {key: payload.get(key, default) for key, default in (("min_bytes", 20), ("min_lines", 1))}
    if any(type(value) is not int or value < 0 for value in options.values()):
        raise ValueError("min_bytes/min_lines 必须是非负整数")
    return accept_files(*names, **options, contains=contains, sha256=hashes)


def _load_acceptance(filename: str | None) -> AcceptanceInput:
    if filename is None:
        return None
    return _file_acceptance(json.loads(Path(filename).read_text(encoding="utf-8-sig")))


def _load_orchestration_acceptance(filename: str | None) -> tuple[AcceptanceInput, dict[str, AcceptanceInput]]:
    if filename is None:
        return None, {}
    payload = json.loads(Path(filename).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or set(payload) - {"root", "subtasks"}:
        raise ValueError("编排验收只接受 root 与 subtasks")
    children = payload.get("subtasks", {})
    if not isinstance(children, dict) or any(not isinstance(key, str) or not key.strip() for key in children):
        raise ValueError("subtasks 必须是以子任务 ID 为键的验收对象")
    return _file_acceptance(payload.get("root")), {
        key: _file_acceptance(value) for key, value in children.items()
    }


def _record_batch_setup_error(tasks: list[dict[str, Any]], run_dir: Path, exc: Exception) -> None:
    with Tracer(run_dir) as tracer:
        for item in tasks:
            task_id = item["task_id"]
            tracer.event("case_registered", schema_version=2, root_id=task_id, task_id=task_id, level="goal", task=item["task"])
            tracer.event(
                "task_end", schema_version=2, level="goal", root_id=task_id,
                task_id=task_id, attempt_id=f"setup-{uuid.uuid4().hex}",
                status="error", error=f"配置错误：{type(exc).__name__}: {exc}",
            )
    render_report(run_dir)


def _load_tasks(path: Path) -> list[dict[str, Any]]:
    """Read a strict case manifest, including optional per-case acceptance.

    Malformed or duplicate cases are rejected rather than silently disappearing
    from the evaluation denominator.
    """
    if not path.exists():
        return []
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_number} 行不是合法 JSON") from exc
        if isinstance(payload, str):
            payload = {"task": payload}
        if not isinstance(payload, dict) or set(payload) - {"task_id", "task", "acceptance"}:
            raise ValueError(f"第 {line_number} 行须为 task_id/task/acceptance 对象")
        task_id = payload.get("task_id", f"task-{line_number}")
        if not isinstance(task_id, str) or not task_id.strip() or task_id in seen:
            raise ValueError(f"第 {line_number} 行 task_id 为空、类型错误或重复")
        if not isinstance(payload.get("task"), str) or not payload["task"].strip():
            raise ValueError(f"第 {line_number} 行缺少有效 task")
        seen.add(task_id)
        tasks.append({**payload, "task_id": task_id})
    return tasks


def _enable_utf8() -> None:
    """Windows 控制台默认不是 UTF-8，中文输出会乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
