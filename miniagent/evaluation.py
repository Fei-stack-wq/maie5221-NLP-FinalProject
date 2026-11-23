"""User-authored checks score a finished delivery on an isolated copy."""
import io
import json
import tempfile
import zipfile
from pathlib import Path
from .sandbox import Sandbox
from .tools import ToolContext
from .shell_tool import run_shell


def validate(checks):
    if not isinstance(checks, list) or len(checks) > 60:
        raise ValueError("Expected up to 60 evaluation checks")
    for check in checks:
        if not isinstance(check, dict) or check.get("kind") not in ("exists", "contains", "command"):
            raise ValueError("Unknown evaluation check")
        key = "command" if check["kind"] == "command" else "path"
        value = check.get(key)
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError("Missing check " + key)
        if key == "path" and (Path(value).is_absolute() or ".." in Path(value).parts):
            raise ValueError("Use a project-relative path")
        if check["kind"] == "contains" and not isinstance(check.get("text"), str):
            raise ValueError("Missing text")
    return checks


def evaluate(store, checks, settings):
    results = []
    if checks:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store.project.checkout(root)
            ctx = ToolContext(Sandbox(root, "evaluation"), settings.limits)
            for check in checks:
                try:
                    if check["kind"] == "command":
                        result = run_shell(ctx, {"command": check["command"]})
                        details = {"passed": result.ok, "exit_code": result.meta.get("exit_code")}
                        if not result.ok:
                            details["output"] = result.content[-1500:]
                    else:
                        path = ctx.sandbox.resolve(check["path"])
                        passed = path.is_file()
                        if check["kind"] == "contains":
                            passed = passed and check["text"] in path.read_text(errors="replace")
                        details = {"passed": passed}
                except (ValueError, OSError) as exc:
                    details = {"passed": False, "error": str(exc)}
                results.append({**check, **details})
    return {"checks":results, "passed":sum(r["passed"] for r in results), "total":len(results)}


def task_changes(store, tid):
    return [c for pub in store.project.history(0) if pub["task_id"] == tid for c in pub["changes"]]


def snapshot(store, tid):
    changes = {c["path"]: c for c in task_changes(store, tid)}
    path = store.task_dir(tid) / "artifacts.zip"
    temporary = path.with_suffix(".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as arc:
        for name, c in changes.items():
            f = store.project.resolve(name)
            if c["change"] != "deleted" and f.is_file():
                arc.write(f, name)
    temporary.replace(path)


def assessor_export(store):
    content = io.BytesIO()
    events = store.chat()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as arc:
        arc.writestr("chat.jsonl", "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))
        lines = ["# " + store.group.name, ""]
        for e in events:
            body = e.get("text") or e.get("task") or e.get("question")
            if body:
                lines.extend(["## " + str(e.get("agent") or e["kind"]), "", body, ""])
        arc.writestr("chat.md", "\n".join(lines))
        for f in store.project.files():
            arc.write(store.project.resolve(f["path"]), "project/" + f["path"])
    return content.getvalue()
