"""Reproducible three-team/two-task runs and independent evidence collection.

The bundled pack is a draft derived from the readiness memo, not Mid_Test.zip.
Live model runs use Studio's real team, human-question and snapshot pathways.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import sys
import time
import uuid
import zipfile

from .config import Settings, configured_limits
from .studio_acceptance import build_checks
from .studio_conversations import validate_conversation


def _path(value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or PurePosixPath(value).is_absolute() or any(p in {"", ".", ".."} or p.startswith(".") for p in value.split("/"))):
        raise ValueError("testpack artifacts require ordinary relative paths")
    return value


def load_pack(path):
    raw = Path(path).read_bytes()
    if len(raw) > 512_000:
        raise ValueError("testpack exceeds 512 KB")
    pack = json.loads(raw)
    if (not isinstance(pack, dict) or pack.get("schema_version") != 1
            or pack.get("source_kind") not in {"derived_document_summary", "original_testpack"}
            or not isinstance(pack.get("source_note"), str) or not pack["source_note"].strip()
            or not isinstance(pack.get("scenarios"), list) or len(pack["scenarios"]) != 3):
        raise ValueError("expected a versioned pack with provenance and exactly three scenarios")
    if pack["source_kind"] == "original_testpack" and not pack.get("original_archive_sha256"):
        raise ValueError("original testpack requires its source archive hash")
    seen = set()
    for scenario in pack["scenarios"]:
        identity = scenario["id"]
        if identity not in {"software", "news", "fund"} or identity in seen:
            raise ValueError("expected unique software, news and fund scenarios")
        seen.add(identity)
        if "memory_enabled" in scenario and type(scenario["memory_enabled"]) is not bool:
            raise ValueError("memory_enabled must be boolean")
        profiles = scenario["profiles"]
        if len(profiles) != 4:
            raise ValueError("each scenario requires four role profiles")
        validate_conversation({"kind": "group", "members": [p["id"] for p in profiles],
                               "profiles": profiles, "recipient": scenario["lead"], "intent": "edit"})
        tasks = scenario["tasks"]
        if not isinstance(tasks, list) or len(tasks) != 2:
            raise ValueError("each scenario requires Task 1 and Task 2")
        task_ids = set()
        for task in tasks:
            if not isinstance(task.get("id"), str) or task["id"] in task_ids:
                raise ValueError("task IDs must be distinct strings")
            task_ids.add(task["id"])
            if not isinstance(task.get("objective"), str) or not 0 < len(task["objective"]) <= 10000:
                raise ValueError("task objective missing or too long")
            if not isinstance(task.get("outputs"), list) or not task["outputs"]:
                raise ValueError("each task requires declared outputs")
            for name in task["outputs"]:
                _path(name)
            build_checks(task["outputs"], task.get("checks", []))
            configured_limits(Settings().limits, task.get("limits", {}))
            if task.get("validator") not in {None, "portfolio", "dashboard", "sources", "slides"}:
                raise ValueError("unsupported independent validator")
    pack["pack_sha256"] = hashlib.sha256(raw).hexdigest()
    return pack


def assess_files(task, files, previous=None):
    """Deterministic structural/numeric checks, never a quality/suitability claim."""
    checks = []
    previous = previous or {}
    def check(name, passed, detail):
        checks.append({"check": name, "passed": bool(passed), "detail": detail})
    for name in task["outputs"]:
        data = files.get(name)
        check("output:" + name, isinstance(data, bytes) and bool(data.strip()), "nonempty delivered file")
        if data is not None and name in previous:
            check("fresh:" + name, data != previous[name], "Task 2 must update outputs declared for this task")
    try:
        validator = task.get("validator")
        if validator == "portfolio":
            value = json.loads(files["portfolio.json"])
            total, assets = value["total"], value["assets"]
            numeric = lambda x: type(x) in (int, float) and math.isfinite(x)
            check("capital", numeric(total) and total > 0, "positive finite simulated capital")
            valid = isinstance(assets, list) and bool(assets) and all(
                numeric(a["weight"]) and numeric(a["amount"]) and 0 <= a["weight"] <= 100 and a["amount"] >= 0 for a in assets)
            check("allocation-types", valid, "finite nonnegative allocations")
            if valid and numeric(total):
                check("weight-total", abs(sum(a["weight"] for a in assets) - 100) < 1e-6, "weights sum to 100 percent")
                check("amount-total", abs(sum(a["amount"] for a in assets) - total) <= .01, "amounts sum to stated capital")
                check("amount-ratios", all(abs(a["amount"] - total * a["weight"] / 100) <= .01 for a in assets), "amounts agree with weights")
        elif validator == "dashboard":
            data = json.loads(files["metrics.json"])
            for key, expected in task.get("expected_metrics", {}).items():
                check("metric:" + key, type(data.get(key)) is type(expected) and data.get(key) == expected, "independent known-record result")
        elif validator == "sources":
            rows = json.loads(files["sources.json"])
            check("source-count", isinstance(rows, list) and len(rows) >= 2, "at least two recorded sources")
            for index, row in enumerate(rows):
                from urllib.parse import urlsplit
                url = urlsplit(row["url"])
                retrieved = datetime.fromisoformat(row["retrieved_at"].replace("Z", "+00:00"))
                published = row["published_at"]
                date.fromisoformat(published) if len(published) == 10 else datetime.fromisoformat(published.replace("Z", "+00:00"))
                check(f"source:{index}", url.scheme == "https" and bool(url.hostname) and retrieved.tzinfo is not None
                      and bool(row.get("published_at")) and bool(row.get("text_path"))
                      and bool(files.get(_path(row["text_path"]))), "URL, dates and fetched body are present; factual quality requires review")
        elif validator == "slides":
            html = files["semiconductors.html"].decode("utf-8")
            check("html-document", "<html" in html.lower() and "</html>" in html.lower(), "complete HTML document")
            check("navigation", "<button" in html.lower() and "<script" in html.lower(), "navigation present; browser evidence is separately required")
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        check("validator-input", False, type(exc).__name__ + ": invalid or missing evidence")
    return {"passed": all(c["passed"] for c in checks), "checks": checks,
            "quality_verified": False, "browser_verified": False}


def collect_turn(service, run_id, task, profiles, previous=None):
    from .studio_service import StudioError
    detail = service.detail(run_id)
    try:
        content = service.artifact_bundle(run_id)
    except StudioError as exc:
        if exc.code != "not_found":
            raise
        files = {}
    else:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    grade = assess_files(task, files, previous)
    reports = [e for e in detail.get("room_events", []) if e["kind"] == "member_report"]
    turn = next(t for t in detail["conversation"]["turns"] if t["run_id"] == run_id)
    participated = {e["role"] for e in reports} | {turn["recipient"]}
    missing = {p["id"] for p in profiles} - participated
    grade["checks"].append({"check": "four-role-participation", "passed": not missing, "detail": sorted(missing)})
    machine = detail["run"]["status"] == "completed" and all(c["passed"] for c in grade["checks"])
    grade.update(machine_passed=machine, passed=machine, run_id=run_id, task_id=task["id"],
                 runtime_status=detail["run"]["status"], human_timing=detail.get("human_timing"),
                 artifacts={name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                 review_status="pending_independent_quality_and_functional_review")
    return grade, files


def run_pack(pack, service, output, *, scenario_id=None, answer=None, poll_interval=.05):
    from .studio_service import StudioError
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    executed = json.dumps({k: v for k, v in pack.items() if k != "pack_sha256"}, ensure_ascii=False, sort_keys=True, indent=2).encode()
    (output / "executed-pack.json").write_bytes(executed)
    report = {"schema_version": 1, "pack_sha256": hashlib.sha256(executed).hexdigest(),
              "input_pack_sha256": pack["pack_sha256"], "execution_kind": pack.get("execution_kind", "live_model"),
              "source_kind": pack["source_kind"],
              "source_note": pack["source_note"], "started_at": datetime.now(timezone.utc).isoformat(),
              "formal_acceptance": False, "scenarios": []}
    def save():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    try:
        for scenario in pack["scenarios"]:
            if scenario_id and scenario["id"] != scenario_id:
                continue
            result = {"id": scenario["id"], "tasks": []}
            report["scenarios"].append(result)
            parent, previous = None, {}
            for task in scenario["tasks"]:
                conversation = ({"parent_run_id": parent, "recipient": scenario["lead"], "intent": "edit", "inherit_files": True}
                                if parent else {"kind": "group", "members": [p["id"] for p in scenario["profiles"]],
                                                "profiles": scenario["profiles"], "recipient": scenario["lead"], "intent": "edit",
                                                "memory_enabled": scenario.get("memory_enabled", False)})
                payload = {"request_id": str(uuid.uuid4()), "mode": "team", "objective": task["objective"],
                           "conversation": conversation, "acceptance_files": task["outputs"],
                           "acceptance_checks": task.get("checks", []),
                           "limits": task.get("limits", {"max_steps": 100, "max_seconds": 300, "max_total_tokens": 200000})}
                run = service.start(payload)
                run_id = run["id"]
                seen = set()
                while True:
                    detail = service.detail(run_id)
                    for question in detail.get("pending_questions", []):
                        if question["request_id"] in seen:
                            continue
                        reply = answer(question) if answer else None
                        if reply is None:
                            service.cancel(run_id)
                            seen.add(question["request_id"])
                        else:
                            try:
                                service.reply_human(run_id, question["request_id"], reply)
                            except StudioError as exc:
                                if exc.status == 400:
                                    # An ambiguous approval is not a decision.
                                    # Keep waiting within the original deadline.
                                    continue
                                if exc.code != "not_waiting":
                                    raise
                            seen.add(question["request_id"])
                    if detail["run"]["status"] not in {"queued", "running", "waiting_user", "created", "ready", "verifying"}:
                        try:
                            grade, files = collect_turn(service, run_id, task, scenario["profiles"], previous)
                        except StudioError as exc:
                            if exc.code != "run_active":
                                raise
                        else:
                            break
                    time.sleep(poll_interval)
                result["tasks"].append(grade)
                (output / f"{scenario['id']}-{len(result['tasks'])}.zip").write_bytes(service.room_bundle(run_id))
                save()
                if not grade["machine_passed"]:
                    result["blocked_followup"] = "Task 1 failed; dependent task was not started" if parent is None else "Task 2 failed"
                    break
                parent, previous = run_id, files
        report["machine_passed"] = (len(report["scenarios"]) == (1 if scenario_id else 3)
                                    and all(len(s["tasks"]) == 2 and all(t["machine_passed"] for t in s["tasks"]) for s in report["scenarios"]))
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["machine_passed"] = False
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "run"])
    parser.add_argument("--pack", type=Path, default=Path(__file__).resolve().parents[1] / "examples/testpack-draft.json")
    parser.add_argument("--runs-dir", type=Path, default=Path("runs/testpack"))
    parser.add_argument("--scenario", choices=["software", "news", "fund"])
    args = parser.parse_args(argv)
    try:
        pack = load_pack(args.pack)
        settings = Settings.from_env()
        if args.action == "check":
            print(json.dumps({"pack_valid": True, "source_kind": pack["source_kind"], "original_tasks_verified": False,
                              "model_configured": bool(settings.api_key), "scenarios": 3, "tasks": 6}, ensure_ascii=False))
            return 0
        if not settings.api_key:
            raise ValueError("缺少模型环境配置。check 可离线使用；run 需要 DEEPSEEK_API_KEY，不能用 mock 冒充正式实测。")
        from .human import TerminalHuman
        from .studio_service import StudioService
        terminal = TerminalHuman()
        run_root = args.runs_dir / ("attempt-" + uuid.uuid4().hex)
        service = StudioService(run_root / "studio", settings)
        try:
            report = run_pack(pack, service, run_root / "evidence", scenario_id=args.scenario,
                              answer=lambda q: terminal.ask.with_timeout(q["text"], q["remaining_seconds"]))
        finally:
            service.close()
        print(json.dumps({"report": str(run_root / "evidence/report.json"), "machine_passed": report["machine_passed"],
                          "formal_acceptance": False}, ensure_ascii=False))
        return 0 if report["machine_passed"] else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"测试包未就绪：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
