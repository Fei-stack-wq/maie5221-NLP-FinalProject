"""Command line: ``python -m miniagent <command>``.

studio   local web Studio (groups, chat, files)
team     run one task for a group from the terminal (answers questions on stdin)
run      single agent on a folder
demo     offline team run with a scripted model (no API key needed)
report   metrics for the tasks of one or all groups
smoke    three-call live protocol probe of the configured model
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import sys
import threading
import time
import webbrowser
from dataclasses import replace
from pathlib import Path

from .config import Settings, load_settings
from .metrics import group_report
from .providers import ScriptedProvider
from .studio import Studio, deepseek_factory

ROOT = Path(__file__).resolve().parent.parent


def _settings(args) -> Settings:
    settings = load_settings(Path(args.env_file) if getattr(args, "env_file", None) else None)
    if getattr(args, "runs_dir", None):
        settings = replace(settings, runs_dir=Path(args.runs_dir))
    if getattr(args, "thinking", None):
        settings = replace(settings, thinking=args.thinking)
    if getattr(args, "max_steps", None):
        settings = settings.with_limits(max_steps=args.max_steps)
    return settings


def _template(name: str) -> dict:
    path = Path(name)
    if not path.exists():
        path = ROOT / "examples" / "teams" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def reopen_studio(directory, args):
    from .instance import PACKAGE, registered_studio, runtime_version, wait_for_instance
    owner = wait_for_instance(directory)
    if not owner:
        return None  # A closing owner can release the data lock without registering again.
    if args.strict_port and owner.port != args.port:
        print(f"此工作室已在 {owner.url} 运行。请先关闭该实例，再用固定端口 {args.port} 启动。", file=sys.stderr)
        return 1
    if not owner.version:
        print("当前实例早于自动更新功能。请在任务结束后关闭旧服务，再双击启动一次。")
    elif owner.version == runtime_version():
        print(f"Studio 已在运行：{owner.url}")
    elif owner.source != str(PACKAGE):
        print(f"此数据目录正由另一份 Studio 代码使用：{owner.source}。请从该目录启动更新。")
    else:
        # Only the verified owner watches this instance-specific local file.
        (directory / (".studio-update-" + owner.id)).touch()
        if owner.busy:
            print("有可用更新。当前任务和对话处理完后会自动切换，期间可继续使用。")
        else:
            print("发现新版，正在等待工作室切换…", flush=True)
            replacement = wait_for_instance(directory, previous=owner.id, timeout=8)
            if replacement:
                if replacement.port != owner.port or replacement.source != owner.source:
                    print("工作室的监听地址或来源在切换期间发生变化，请重新启动。", file=sys.stderr)
                    return 1
                owner = replacement
            else:
                current = registered_studio(directory)
                if not current:
                    print("新版尚未就绪，请查看 Studio 启动窗口。", file=sys.stderr)
                    return 1
                owner = current
                print("工作室仍在处理请求，会在空闲时自动切换。")
    if not args.no_browser:
        webbrowser.open(owner.url)
    return 0


def cmd_studio(args) -> int:
    import signal
    settings = _settings(args)
    from .instance import RestartStudio, advertised_studio
    from .server import serve
    studio = Studio(settings)
    studio.env_file = Path(args.env_file or ".env").resolve()
    try:
        main, preview = serve(studio, args.port, strict_port=args.strict_port)
    except OSError as exc:
        studio.close()
        if args.strict_port and exc.errno == errno.EADDRINUSE:
            print(f"端口 {args.port} 已被占用。请先关闭占用该端口的服务，再启动工作室。", file=sys.stderr)
            return 1
        raise
    try:
        with advertised_studio(settings.runs_dir, main.server_port, main.RequestHandlerClass.instance_id,
                               main.RequestHandlerClass.build_id) as update:
            studio.restore_queue()
            url = f"http://127.0.0.1:{main.server_address[1]}/"
            print(f"miniagent Studio: {url}  (project previews on port {preview.server_address[1]})")
            print(f"model {settings.model}, thinking {settings.thinking}, runs in {settings.runs_dir.resolve()}")
            if not settings.api_key:
                print("warning: DEEPSEEK_API_KEY is not set (.env or environment); tasks cannot start.")
            if not args.no_browser:
                threading.Timer(0.6, lambda: webbrowser.open(url)).start()
            main_thread = threading.current_thread() is threading.main_thread()
            if main_thread:
                previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
            try:
                while not update.exists():
                    time.sleep(0.4)
                studio.drain_for_update()
                raise RestartStudio(main.server_port)
            finally:
                if main_thread:
                    signal.signal(signal.SIGTERM, previous)
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        studio.close()
        main.shutdown()
        preview.shutdown()
        main.server_close()
        preview.server_close()
    return 0


def _print_event(event: dict, names: dict) -> None:
    kind = event["kind"]
    name = lambda agent: names.get(agent, agent)  # noqa: E731
    if kind == "user_message":
        print(f"\n[you] {event['text']}")
    elif kind == "room_reply":
        print(f"\n[{name(event['agent'])}] {event['text']}")
    elif kind == "delegation":
        print(f"\n[{name(event['agent'])} → {name(event['assignee'])}] {event['task']}")
    elif kind == "report":
        files = ", ".join(c["path"] for c in event.get("changes", []))
        print(f"\n[{name(event['agent'])} report · {event['status']}] {event['text']}" + (f"\n  files: {files}" if files else ""))
    elif kind == "question":
        print(f"\n[{name(event['agent'])} asks you] {event['question']}")
    elif kind == "answer":
        print(f"\n[your answer] {event['text']}")
    elif kind == "final":
        print(f"\n[{name(event['agent'])} · final · {event['status']}]\n{event['text']}")
    elif kind == "task_finished":
        print(f"\n— task {event['status']} — {json.dumps(event.get('metrics', {}), ensure_ascii=False)}")


def _drive(studio: Studio, group_id: str, text: str, *, read_answers: bool = True, conversation: bool = False) -> dict:
    store = studio.group(group_id)
    names = {m.id: m.name for m in store.group.members}
    after = store.chat()[-1]["seq"] if store.chat() else 0
    route = studio.post(group_id, text, queue=not conversation)
    if route["route"] == "reply":
        return route
    answered: set[str] = set()
    while True:
        state = studio.state(group_id, after)
        for event in state["events"]:
            after = event["seq"]
            _print_event(event, names)
        live = state["live"]
        if live is None:
            break
        question = live.get("question")
        if question and question["id"] not in answered and read_answers:
            answered.add(question["id"])
            print("\nyour answer (end with an empty line):")
            lines = []
            for line in sys.stdin:
                if not line.strip():
                    break
                lines.append(line.rstrip("\n"))
            result = studio.post(group_id, "\n".join(lines) or "Please decide yourselves and state your assumptions.")
            if result["route"] != "answer":
                answered.discard(question["id"])
        time.sleep(0.5)
    return studio.store.get(group_id).load_task(route["task_id"])


def cmd_team(args) -> int:
    settings = _settings(args)
    studio = Studio(settings)
    if args.group:
        group_id = args.group
    else:
        template = _template(args.template)
        group = studio.create_group(template["name"], template["members"], template["lead"],
                                    "autonomous" if args.autonomous else "interactive")
        group_id = group["id"]
        print(f"created group {group_id}: {template['name']}")
    if args.message:
        text = args.message
    else:
        template = _template(args.template)
        text = template["tasks"][args.task - 1]["text"]
    record = _drive(studio, group_id, text, conversation=bool(args.group and args.message))
    if record.get("route") == "reply":
        print(record["text"])
        return 0
    print(f"\ngroup {group_id} · task {record['id']} · {record['status']}")
    print(f"project: {studio.group(group_id).project.root}")
    return 0 if record["status"] == "completed" else 1


def cmd_run(args) -> int:
    settings = _settings(args)
    studio = Studio(settings)
    group = studio.create_group(args.name, [{"id": "agent", "name": "Agent", "prompt": args.role}], "agent",
                                "autonomous" if args.autonomous else "interactive")
    record = _drive(studio, group["id"], args.task)
    print(f"\nproject: {studio.group(group['id']).project.root}")
    return 0 if record["status"] == "completed" else 1


def cmd_demo(args) -> int:
    settings = replace(_settings(args), api_key="")
    script = json.loads((ROOT / "examples" / "demo_team.json").read_text(encoding="utf-8"))
    studio = Studio(settings, provider_factory=lambda _s: ScriptedProvider(script))
    group = studio.create_group("Offline demo", [
        {"id": "lead", "name": "Lead", "prompt": "Coordinates the work."},
        {"id": "writer", "name": "Writer", "prompt": "Writes clear notes."},
        {"id": "reviewer", "name": "Reviewer", "prompt": "Checks the notes independently."},
    ], "lead", "autonomous")
    record = _drive(studio, group["id"], "Write short notes on the ReAct pattern in notes.md and have them reviewed.",
                    read_answers=False)
    project = studio.group(group["id"]).project
    print(f"\nproject files: {[f['path'] for f in project.files()]}")
    print(f"project: {project.root}")
    return 0 if record["status"] == "completed" else 1


def cmd_report(args) -> int:
    settings = _settings(args)
    studio = Studio(settings)
    groups = [studio.group(args.group)] if args.group else [studio.group(g.id) for g in studio.store.conversations()]
    for store in groups:
        print(group_report(store))
        print()
    return 0


def cmd_smoke(args) -> int:
    from .provider_smoke import run_provider_smoke
    settings = _settings(args)
    report = run_provider_smoke(deepseek_factory(settings), live=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


def cmd_migrate(args) -> int:
    from .roles import Roles
    roles = Roles(_settings(args).runs_dir)
    print(json.dumps(roles.migrate(Path(args.source)), ensure_ascii=False))
    return 0


def cmd_eval(args) -> int:
    from .metrics import summarize
    from .evaluation import validate
    settings = _settings(args)
    rows = [json.loads(line) for line in Path(args.tasks).read_text().splitlines() if line.strip()]
    for row in rows:
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ValueError("Every evaluation task needs text")
        validate(row.get("checks", []))
    studio = Studio(settings)
    records, groups = [], {}
    try:
        for row in rows:
            key = row.get("group")
            if key is None or key not in groups:
                template = _template(row["template"]) if row.get("template") else {
                    "name":"Evaluation","lead":"agent","members":[{"id":"agent","name":"Agent","prompt":""}]}
                gid = studio.create_group(template["name"], template["members"], template["lead"], "autonomous")["id"]
                if key is not None:
                    groups[key] = gid
            else:
                gid = groups[key]
            tid = studio.post(gid, row["text"], queue=True, options={"checks":row.get("checks",[])})["task_id"]
            studio.wait(gid)
            records.append(studio.group(gid).load_task(tid))
        print(json.dumps({"summary":summarize(records),
                          "tasks":[{k:r.get(k) for k in ("id","group_id","status","evaluation")} for r in records]},
                         ensure_ascii=False, indent=2))
    finally:
        studio.close()
    return 0


def cmd_bench(args) -> int:
    from .bench import ARMS, Bench
    settings = _settings(args)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        raise SystemExit(f"unknown arm(s): {', '.join(unknown)}; choose from {', '.join(ARMS)}")
    if not settings.api_key:
        raise SystemExit("DEEPSEEK_API_KEY is not set; the comparison needs the real model")
    scenarios = [f"scenario_{n.strip()}" for n in args.scenarios.split(",") if n.strip()]
    templates = {name: _template(name) for name in scenarios}
    tasks = [int(n) for n in args.tasks.split(",") if n.strip()] if args.tasks else None
    if tasks and tasks != [1] and 1 not in tasks:
        raise SystemExit("Task 2 builds on Task 1 in the same group; include task 1")
    meta = Bench(settings, templates, arms, Path(args.out), solo_max_steps=args.solo_max_steps,
                 min_balance=args.min_balance, tasks=tasks).run(scenarios)
    print((Path(args.out) / "summary.md").read_text(encoding="utf-8"))
    print(json.dumps({k: meta.get(k) for k in ("balance_before", "balance_after")}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m miniagent", description="miniagent multi-agent Studio")
    parser.add_argument("--env-file", help="read settings from this .env (default: ./.env if present)")
    parser.add_argument("--runs-dir", help="where groups and runs are stored (default: runs)")
    parser.add_argument("--thinking", choices=["enabled", "disabled"], help="override DEEPSEEK_THINKING")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("studio", help="start the local web Studio")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--strict-port", action="store_true", help="require the requested main port, including on reuse")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--reuse", action="store_true", help="open the existing Studio for this data directory if running")
    p.set_defaults(fn=cmd_studio)

    p = sub.add_parser("team", help="run one group task in the terminal")
    p.add_argument("--template", default="scenario_1", help="examples/teams/<name>.json or a path")
    p.add_argument("--group", help="existing group id (continue its conversation)")
    p.add_argument("--task", type=int, default=1, help="task number from the template")
    p.add_argument("--message", help="message text instead of a template task")
    p.add_argument("--autonomous", action="store_true", help="the team may not ask questions")
    p.add_argument("--max-steps", type=int, help="member step budget")
    p.set_defaults(fn=cmd_team)

    p = sub.add_parser("run", help="single agent")
    p.add_argument("task")
    p.add_argument("--name", default="Single agent")
    p.add_argument("--role", default="You are a capable general assistant.")
    p.add_argument("--autonomous", action="store_true")
    p.add_argument("--max-steps", type=int)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("demo", help="offline scripted team run")
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("report", help="task metrics")
    p.add_argument("--group")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("migrate", help="import legacy role definitions and approved knowledge")
    p.add_argument("--source", required=True)
    p.set_defaults(fn=cmd_migrate)
    p = sub.add_parser("eval", help="run JSONL evaluation tasks")
    p.add_argument("--tasks", required=True)
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("bench", help="compare team set-ups and a single agent on the Mid_Test scenarios")
    p.add_argument("--out", required=True, help="directory for results, copied projects and chats")
    p.add_argument("--arms", default="team,team-classic,solo",
                   help="comma-separated: team, team-fresh, team-classic, solo")
    p.add_argument("--scenarios", default="1,2,3", help="scenario numbers, e.g. 1,2,3")
    p.add_argument("--solo-max-steps", type=int, default=400)
    p.add_argument("--tasks", default="", help="task numbers to run per scenario, e.g. 1 (default: all)")
    p.add_argument("--min-balance", type=float, default=5.0,
                   help="do not start another task when the DeepSeek balance is below this amount")
    p.set_defaults(fn=cmd_bench)
    p = sub.add_parser("smoke", help="live provider protocol probe")
    p.set_defaults(fn=cmd_smoke)

    args = parser.parse_args(argv)
    if args.command == "studio" and args.strict_port and not 1 <= args.port <= 65535:
        parser.error("--strict-port requires a port between 1 and 65535")
    if args.command in {"studio", "demo", "run", "team", "migrate", "eval"}:
        from .instance import RestartStudio, session_lock, StudioInUse
        directory = _settings(args).runs_dir
        try:
            for _ in range(2):
                try:
                    with session_lock(directory):
                        return args.fn(args)
                except StudioInUse as exc:
                    if args.command != "studio" or not args.reuse:
                        print(str(exc), file=sys.stderr)
                        return 1
                    reopened = reopen_studio(directory, args)
                    if reopened is not None:
                        return reopened
            print(f"数据目录正在使用：{directory.resolve()}。请查看已运行的 Studio 窗口或稍后重试。", file=sys.stderr)
            return 1
        except RestartStudio as restart:
            # The old listeners and directory lock are now released. Re-enter normally so a
            # simultaneous double-click still has to win the same exclusive data lock.
            launch = [sys.executable, "-u", "-m", "miniagent", "--runs-dir", str(directory.resolve())]
            if args.env_file:
                launch.extend(["--env-file", args.env_file])
            if args.thinking:
                launch.extend(["--thinking", args.thinking])
            os.execv(sys.executable, [*launch, "studio", "--port", str(restart.args[0]),
                                      "--strict-port", "--reuse", "--no-browser"])
    return args.fn(args)
