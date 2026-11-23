"""Compare collaboration set-ups on the Mid_Test scenarios with the real model.

The arms run the same two tasks per scenario in one group each, in autonomous mode:

* ``team``         — the scenario's four roles with every collaboration feature on: continued
                     sessions, parallel hand-offs, review loops, checks and the lead's plan.
* ``team-fresh``   — the same, except every assignment starts a new conversation.
* ``team-classic`` — the same roles with all of those features off (the behaviour before them).
* ``solo``         — one agent holding all four role descriptions, with a larger step budget.
* ``fast``         — the four roles in fast mode (lead and reviewers think, workers do not, 15-minute limit).

Post-task lessons are switched off in every arm: nobody approves them during a benchmark.

Scenario 1 (a web app that agents start and test on localhost) runs one arm at a
time, because agent sandboxes do not isolate network ports. The other scenarios
run in parallel. Each finished task appends one JSON line to ``results.jsonl`` and
copies its project and chat into ``<out>/<arm>/<scenario>-task<n>/`` for review.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Settings, fast_profile
from .studio import Studio

ARMS = ("team", "team-fresh", "team-classic", "solo", "fast")
EXCLUSIVE = {"scenario_1"}
_COPY_IGNORE = shutil.ignore_patterns(".git", "__pycache__", "node_modules", ".venv", "*.pyc")


def solo_member(template: dict) -> dict:
    roles = "\n".join(f"- {m['name']}: {m['prompt']}" for m in template["members"])
    return {"id": "agent", "name": f"{template['name']} (solo)",
            "prompt": "You are the whole team below. Do the work of every role yourself, including the "
                      "independent checks another role would normally make.\n" + roles}


def arm_settings(settings: Settings, arm: str, solo_max_steps: int) -> Settings:
    on = dict(resume_members=True, parallel_delegations=True, review_loops=True, plans=True, retrospective=False)
    if arm == "team":
        return replace(settings, team=replace(settings.team, **on))
    if arm == "team-fresh":
        return replace(settings, team=replace(settings.team, **{**on, "resume_members": False}))
    if arm == "team-classic":
        return replace(settings, team=replace(settings.team, **{key: False for key in on}))
    if arm == "fast":
        return fast_profile(replace(settings, team=replace(settings.team, **on)))
    if arm == "solo":
        return replace(settings.with_limits(max_steps=solo_max_steps),
                       team=replace(settings.team, retrospective=False))
    raise ValueError(f"unknown arm {arm!r}; choose from {', '.join(ARMS)}")


def balance(settings: Settings) -> Any:
    """DeepSeek account balance, or None when the endpoint is unavailable."""
    if not settings.api_key:
        return None
    request = urllib.request.Request(settings.base_url.rstrip("/") + "/user/balance",
                                     headers={"Authorization": f"Bearer {settings.api_key}",
                                              "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [{k: info.get(k) for k in ("currency", "total_balance")} for info in data.get("balance_infos", [])]
    except Exception:  # noqa: BLE001 - informational only
        return None


class Bench:
    def __init__(self, settings: Settings, templates: dict[str, dict], arms: list[str], out: Path,
                 solo_max_steps: int = 400, provider_factory=None, min_balance: float = 0.0,
                 tasks: list[int] | None = None) -> None:
        self.tasks = tasks
        self.provider_factory = provider_factory
        self.min_balance = min_balance
        self.settings = settings
        self.templates = templates
        self.arms = arms
        self.out = out
        self.solo_max_steps = solo_max_steps
        self.lock = threading.Lock()
        self.exclusive = {name: threading.Lock() for name in EXCLUSIVE}

    def funds(self) -> float | None:
        """Lowest remaining balance across currencies, when a minimum is set and the API reports one."""
        if not self.min_balance:
            return None
        info = balance(self.settings)
        if not info:
            return None
        try:
            return min(float(item["total_balance"]) for item in info)
        except (KeyError, TypeError, ValueError):
            return None

    def record(self, row: dict) -> None:
        with self.lock:
            with (self.out / "results.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"[{time.strftime('%H:%M:%S')}] {row['arm']:<10} {row['scenario']} task {row['task']}: "
                  f"{row['status']} · {row.get('minutes', 0):.1f} min · {row.get('prompt_tokens', 0):,} prompt tokens",
                  flush=True)

    def chain(self, studio: Studio, arm: str, name: str) -> None:
        guard = self.exclusive.get(name)
        if guard:
            guard.acquire()
        try:
            self._chain(studio, arm, name)
        finally:
            if guard:
                guard.release()

    def _chain(self, studio: Studio, arm: str, name: str) -> None:
        template = self.templates[name]
        if arm == "solo":
            gid = studio.create_group(f"{template['name']} · solo", [solo_member(template)], "agent", "autonomous")["id"]
        else:
            gid = studio.create_group(template["name"], template["members"], template["lead"], "autonomous")["id"]
        for index, task in enumerate(template["tasks"], 1):
            if self.tasks and index not in self.tasks:
                continue
            started = time.monotonic()
            row: dict[str, Any] = {"arm": arm, "scenario": name, "task": index, "group_id": gid,
                                   "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            left = self.funds()
            if left is not None and left < self.min_balance:
                row.update(status="skipped_low_balance", error=f"balance {left} below {self.min_balance}")
                self.record(row)
                return
            try:
                tid = studio.post(gid, task["text"], queue=True)["task_id"]
                studio.wait(gid)
                record = studio.group(gid).load_task(tid)
            except Exception as exc:  # noqa: BLE001 - one failed chain must not stop the others
                row.update(status="harness_error", error=f"{type(exc).__name__}: {exc}",
                           minutes=round((time.monotonic() - started) / 60, 1))
                self.record(row)
                return
            store = studio.group(gid)
            dest = self.out / arm / f"{name}-task{index}"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(store.project.root, dest / "project", ignore=_COPY_IGNORE)
            for extra in ("chat.jsonl",):
                source = store.root / extra
                if source.exists():
                    shutil.copy2(source, dest / extra)
            metrics = record.get("metrics") or {}
            (dest / "final_answer.md").write_text(record.get("final_answer") or "", encoding="utf-8")
            row.update(task_id=record["id"], status=record["status"], error=record.get("error", ""),
                       minutes=round(metrics.get("duration_s", time.monotonic() - started) / 60, 1),
                       steps=metrics.get("steps", 0), tool_calls=metrics.get("tool_calls", 0),
                       delegations=metrics.get("delegations", 0),
                       resumed_assignments=metrics.get("resumed_assignments", 0),
                       prompt_tokens=metrics.get("prompt_tokens", 0),
                       prompt_cache_hit_tokens=metrics.get("prompt_cache_hit_tokens", 0),
                       completion_tokens=metrics.get("completion_tokens", 0),
                       model_minutes=round((metrics.get("time") or {}).get("model_s", 0) / 60, 1),
                       tool_minutes=round((metrics.get("time") or {}).get("tool_s", 0) / 60, 1),
                       cost_usd=(metrics.get("time") or {}).get("cost_usd", 0),
                       parallel_reads=metrics.get("parallel_reads", 0),
                       files=len(store.project.files()),
                       assignments=[{k: a.get(k) for k in ("agent", "status", "steps", "prompt_tokens", "resumed")}
                                    for a in record.get("assignments", [])])
            self.record(row)
            if record["status"] in ("failed", "cancelled"):
                return

    def run(self, scenarios: list[str]) -> dict:
        self.out.mkdir(parents=True, exist_ok=True)
        meta = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "arms": self.arms, "scenarios": scenarios,
                "tasks": self.tasks or "all",
                "model": self.settings.model, "thinking": self.settings.thinking,
                "limits": {"member_max_steps": self.settings.limits.max_steps,
                           "lead_max_steps": self.settings.team.lead_max_steps,
                           "max_delegations": self.settings.team.max_delegations,
                           "solo_max_steps": self.solo_max_steps},
                "balance_before": balance(self.settings)}
        (self.out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        studios, threads = [], []
        for arm in self.arms:
            studio = Studio(replace(arm_settings(self.settings, arm, self.solo_max_steps), runs_dir=self.out / arm / "runs"),
                            provider_factory=self.provider_factory)
            studios.append(studio)
            for name in scenarios:
                thread = threading.Thread(target=self.chain, args=(studio, arm, name), name=f"{arm}-{name}")
                thread.start()
                threads.append(thread)
        for thread in threads:
            thread.join()
        for studio in studios:
            studio.close()
        meta.update(finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), balance_after=balance(self.settings))
        (self.out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = summarize(self.out)
        (self.out / "summary.md").write_text(summary, encoding="utf-8")
        return meta


def summarize(out: Path) -> str:
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    lines = ["| Arm | Scenario | Task | Status | Minutes | Model / tool min | Steps | Hand-offs (continued) | "
             "Prompt tokens | Cache hit | Est. USD | Files |",
             "| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in sorted(rows, key=lambda r: (r["arm"], r["scenario"], r["task"])):
        prompt = row.get("prompt_tokens", 0)
        hit = row.get("prompt_cache_hit_tokens", 0)
        lines.append(f"| {row['arm']} | {row['scenario']} | {row['task']} | {row['status']} | {row.get('minutes', 0)} | "
                     f"{row.get('model_minutes', 0)} / {row.get('tool_minutes', 0)} | "
                     f"{row.get('steps', 0)} | {row.get('delegations', 0)} ({row.get('resumed_assignments', 0)}) | "
                     f"{prompt:,} | {(100 * hit / prompt if prompt else 0):.0f}% | {row.get('cost_usd', 0):.2f} | "
                     f"{row.get('files', 0)} |")
    lines += ["", "| Arm | Tasks completed | Minutes | Prompt tokens | Completion tokens |", "| --- | ---: | ---: | ---: | ---: |"]
    for arm in sorted({r["arm"] for r in rows}):
        mine = [r for r in rows if r["arm"] == arm]
        lines.append(f"| {arm} | {sum(r['status'] == 'completed' for r in mine)}/{len(mine)} | "
                     f"{sum(r.get('minutes', 0) for r in mine):.1f} | {sum(r.get('prompt_tokens', 0) for r in mine):,} | "
                     f"{sum(r.get('completion_tokens', 0) for r in mine):,} |")
    return "\n".join(lines) + "\n"
