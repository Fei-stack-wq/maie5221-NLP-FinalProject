"""Local Studio application service, sharing the existing execution lifecycle.

The web layer owns job admission and presentation metadata, never TaskRecord
state. Every CLI-style run retains its own TaskStore. Losing this process does
not resume tools: unfinished jobs are displayed as requiring verification
(``interrupted`` on the wire), without claiming the previous executor is dead.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import tempfile
import traceback
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from .acceptance import accept_files
from .agent import Agent, COORDINATOR_PROMPT
from .config import Settings, configured_limits
from .contracts.acceptance import AcceptanceReport, AcceptanceSpec, validate_report
from .acceptance_evidence import validate_evidence
from .contracts.tasks import AttemptRecord, TaskRecord
from .metrics import Tracer, render_report, summarize
from .orchestrator import Orchestrator
from .providers import DeepSeekProvider, ScriptedProvider
from .sandbox import Sandbox
from .task_service import TaskService
from .task_store import StoreTransaction, TaskStore, _schema
from .tools import build_default_registry
from .project_data import (PROJECT_CACHE_DIRS, SQLITE_EXTENSIONS, SQLITE_SIDECARS, is_database, is_png,
                           supported_project_file, validate_project_data)
from .studio_roles import RoleSession, StudioRoles, knowledge_project
from .studio_role_memory import RoleMemoryStore
from . import studio_workspace
from .studio_discussion import RoomDiscussion
from .studio_diagnostics import diagnose
from .studio_acceptance import build_checks
from .studio_crew import RoomCrew
from .studio_conversations import (ROLE_LABELS, SnapshotError, copy_project, project_context,
                                   thread_info, validate_conversation)

MAX_PREVIEW_BYTES = 262144
MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 300
MAX_ARTIFACTS = 500
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_TERMINAL = {"completed", "unverified", "failed", "cancelled"}
_EXTENSIONS = {".md", ".txt", ".rst", ".json", ".jsonl", ".log", ".csv", ".tsv", ".sql",
               ".png", ".py", ".html", ".css", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
               ".sh", ".toml", ".yaml", ".yml", ".go", ".mod", ".sum", ".lock", ".c", ".h",
               ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".rs", ".java", ".mk", ".cmake",
               "Makefile", "Dockerfile"} | SQLITE_EXTENSIONS
_MODES = [
    {"id": "demo", "label": "单 Agent 演示", "description": "离线生成笔记，执行工具并独立验收"},
    {"id": "team-demo", "label": "协作演示", "description": "离线体验 worker 与 writer 的依赖协作"},
    {"id": "single", "label": "单 Agent 任务", "description": "使用本机配置的 DeepSeek 执行自定义任务"},
    {"id": "team", "label": "多 Agent 任务", "description": "由 DeepSeek 规划任务图并安排协作"},
]
_DEMO_TITLES = {"demo": "生成笔记并验证工具执行", "team-demo": "整理笔记并写摘要"}


class StudioError(ValueError):
    def __init__(self, message: str, code: str = "invalid_request", status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _relative(name: str) -> tuple[str, ...]:
    if type(name) is not str or not name or len(name) > 2048 or "\\" in name or "\x00" in name:
        raise StudioError("文件路径无效。", "invalid_path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} or part.startswith(".") for part in name.split("/")):
        raise StudioError("仅可读取当前运行的产物。", "invalid_path")
    return path.parts


def _read_file(root: Path, relative: str, limit: int, *, missing: bool = False, tail: bool = False) -> tuple[bytes, bool]:
    """Bounded no-follow reads, including every intermediate directory."""
    parts = _relative(relative)
    descriptors: list[int] = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
        file = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(file)
        info = os.fstat(file)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StudioError("只能预览普通文本文件。", "invalid_path")
        offset = max(0, info.st_size - limit) if tail else 0
        starts_midline = False
        if offset:
            os.lseek(file, offset - 1, os.SEEK_SET)
            starts_midline = os.read(file, 1) != b"\n"
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(file, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        truncated = bool(offset) or len(data) > limit
        data = data[:limit]
        if starts_midline:
            # The oldest line in a bounded tail may start outside our window.
            # Never interpret its suffix as a standalone JSON event.
            newline = data.find(b"\n")
            data = data[newline + 1:] if newline >= 0 else b""
        return data, truncated
    except FileNotFoundError:
        if missing:
            return b"", False
        raise StudioError("产物不存在。", "not_found", 404) from None
    except OSError:
        raise StudioError("文件不可读取，符号链接不受支持。", "invalid_path") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _write_json(directory: Path, name: str, value: Any) -> None:
    temporary = directory / (name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / name)
    finally:
        temporary.unlink(missing_ok=True)


class StudioService(RoomDiscussion, StudioRoles):
    def __init__(self, runs_dir: Path | str, settings: Settings, *, max_workers: int = 2):
        if type(max_workers) is not int or not 1 <= max_workers <= 8:
            raise ValueError("max_workers must be between 1 and 8")
        supplied = Path(runs_dir).expanduser()
        if supplied.is_symlink():
            raise StudioError("运行根目录不能是符号链接。", "invalid_path")
        supplied.mkdir(parents=True, exist_ok=True)
        self.runs_dir = supplied.resolve()
        self.settings = settings
        self.role_memory = RoleMemoryStore(self.runs_dir / "studio-role-memory.sqlite3", secret=settings.api_key)
        self.max_workers = max_workers
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="studio-job")
        self._slots = threading.BoundedSemaphore(max_workers)
        self._active: set[str] = set()
        self._cancellations: dict[str, threading.Event] = {}
        self._answers = threading.Condition(self._lock)
        self._questions = {}
        self._closed = False

    def profiles(self):
        data, truncated = _read_file(self.runs_dir, "studio-profiles.json", 2 * 1024**2, missing=True)
        if truncated:
            raise StudioError("角色资料超过存储上限。", "profile_storage", 409)
        return json.loads(data) if data else []

    def add_profile(self, profile):
        if not isinstance(profile, dict):
            raise StudioError("请填写角色资料。")
        identity = profile.get("id")
        try:
            validate_conversation({"kind": "dm", "members": [identity], "recipient": identity,
                                   "profiles": [profile], "intent": "edit"})
        except ValueError as exc:
            raise StudioError(str(exc)) from None
        with self._lock, self._admission():
            rows = self.profiles()
            if profile in rows:
                return self._redact(profile)
            if identity.lower() in {key.lower() for key in ROLE_LABELS} | {row["id"].lower() for row in rows}:
                raise StudioError("角色 ID 已被使用。", "profile_conflict", 409)
            updated = [*rows, self._redact(profile)]
            if len(json.dumps(updated, ensure_ascii=False).encode()) > 2 * 1024**2:
                raise StudioError("角色资料超过存储上限。", "profile_storage", 409)
            _write_json(self.runs_dir, "studio-profiles.json", updated)
            return updated[-1]

    def bootstrap(self) -> dict[str, Any]:
        return {"provider_ready": bool(self.settings.api_key.strip()), "model": self.settings.model,
                "thinking": self.settings.thinking, "max_output_tokens": self.settings.max_output_tokens,
                "limits": {"max_steps": self.settings.limits.max_steps,
                           "max_total_tokens": self.settings.limits.max_total_tokens,
                           "max_seconds": self.settings.limits.max_seconds},
                "local_workspaces": True, "custom_profiles": self.profiles(),
                "conversation_roles": [{"id": key, "label": label} for key, label in ROLE_LABELS.items()],
                "modes": [dict(mode) for mode in _MODES]}

    def close(self, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
            self._answers.notify_all()
        self._executor.shutdown(wait=wait)

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            key = self.settings.api_key
            return value.replace(key, "[REDACTED]") if key else value
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {key: ("[REDACTED]" if key.lower() in {"api_key", "authorization", "password", "secret"}
                          else self._redact(item)) for key, item in value.items()}
        return value

    def _directory(self, run_id: str) -> Path:
        if type(run_id) is not str or not _ID.fullmatch(run_id) or run_id in {".", ".."}:
            raise StudioError("运行标识无效。", "not_found", 404)
        path = self.runs_dir / run_id
        if path.is_symlink() or not path.is_dir():
            raise StudioError("运行不存在。", "not_found", 404)
        return path

    def _directories(self) -> list[Path]:
        return sorted((path for path in self.runs_dir.iterdir()
                       if not path.is_symlink() and path.is_dir() and _ID.fullmatch(path.name)
                       and ((path / "tasks.sqlite3").is_file() or (path / "studio.json").is_file()
                            or (path / "trace.jsonl").is_file())), key=lambda path: path.name, reverse=True)

    def _meta(self, path: Path) -> dict[str, Any]:
        data, truncated = _read_file(path, "studio.json", MAX_PREVIEW_BYTES, missing=True)
        if not data:
            return {}
        try:
            value = json.loads(data)
            if truncated or not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise StudioError("运行元数据无法读取。", "storage_error", 409) from None

    def _update_meta(self, run_id: str, **updates: Any) -> None:
        with self._lock:
            path = self._directory(run_id)
            meta = self._meta(path)
            meta.update(updates)
            _write_json(path, "studio.json", meta)

    def _records(self, path: Path) -> tuple[list[dict], list[dict]]:
        """Read the existing schema without constructing/mutating TaskStore."""
        database = path / "tasks.sqlite3"
        if database.is_symlink():
            raise StudioError("任务数据库无法读取。", "storage_error", 409)
        if not database.exists():
            return [], []
        if not database.is_file():
            raise StudioError("任务数据库无法读取。", "storage_error", 409)
        for name in ("tasks.sqlite3-wal", "tasks.sqlite3-shm"):
            if (path / name).is_symlink():
                raise StudioError("任务数据库无法读取。", "storage_error", 409)
        connection = None
        try:
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            # Reuse the store's schema and graph invariants inside this read-only
            # snapshot, without its constructor's initialization/WAL side effects.
            _schema(connection)
            snapshot = StoreTransaction(connection, writable=False)
            snapshot._validate()
            rows = connection.execute(
                "SELECT kind,id,version,payload FROM records WHERE kind IN ('TaskRecord','AttemptRecord','AcceptanceReport','AcceptanceSpec')"
            ).fetchall()
            tasks, reports, specs, attempts = [], [], {}, {}
            for kind, identity, version, payload in rows:
                raw = json.loads(payload)
                if kind == "TaskRecord":
                    tasks.append(TaskRecord.model_validate(raw).model_dump(mode="json"))
                elif kind == "AttemptRecord":
                    attempts[identity] = AttemptRecord.model_validate(raw)
                elif kind == "AcceptanceReport":
                    reports.append(AcceptanceReport.model_validate(raw))
                else:
                    specs[(identity, version)] = AcceptanceSpec.model_validate(raw)
            acceptance = []
            task_ids = {task["task_id"] for task in tasks}
            for report in sorted(reports, key=lambda report: report.created_at):
                reference = report.spec_ref
                spec = specs.get((reference.id, reference.version)) if reference else None
                attempt = attempts.get(report.attempt_id)
                if (report.task_id not in task_ids or attempt is None
                        or attempt.task_id != report.task_id or report.report_id not in attempt.report_ids):
                    raise ValueError("report task/attempt binding is incomplete")
                validate_report(spec, report)
                blob = connection.execute("SELECT kind,payload FROM blobs WHERE blob_id=?",
                                          ("acceptance-evidence:" + report.report_id,)).fetchone()
                evidence = json.loads(blob[1]) if blob else None
                if (blob is None or blob[0] != "acceptance_evidence" or not isinstance(evidence, dict)
                        or evidence.get("task_id") != report.task_id
                        or evidence.get("attempt_id") != report.attempt_id
                        or evidence.get("report_id") != report.report_id
                        or not isinstance(evidence.get("receipts"), list)):
                    raise ValueError("report evidence binding is incomplete")
                receipts = evidence["receipts"]
                if report.verdict == "passed":
                    validate_evidence(spec, report, receipts)
                acceptance.append({"task_id": report.task_id, "report": report.model_dump(mode="json"),
                                   "spec": spec.model_dump(mode="json") if spec else None,
                                   "receipts": receipts})
            return sorted(tasks, key=lambda task: task["created_at"]), acceptance
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            raise StudioError("任务数据库无法读取，原始记录未被修改。", "storage_error", 409) from None
        finally:
            if connection is not None:
                connection.close()

    def _events(self, path: Path) -> tuple[list[dict], bool]:
        data, truncated = _read_file(path, "trace.jsonl", MAX_LOG_BYTES, missing=True, tail=True)
        events = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    events.append(value)
            except ValueError:
                pass  # A writer may currently be appending its last line.
        return events, truncated

    def _artifacts(self, path: Path, tasks: list[dict]) -> list[dict]:
        # Only known workspace roots are visible. Control files and hidden files
        # never become artifacts, even if a user guesses their URL.
        workspaces = []
        agents = path / "agents"
        if agents.is_dir() and not agents.is_symlink():
            workspaces.append(agents)
        root = path / "workspaces"
        if root.is_dir() and not root.is_symlink():
            for attempt in root.iterdir():
                child = attempt / "agents"
                if attempt.is_dir() and not attempt.is_symlink() and child.is_dir() and not child.is_symlink():
                    workspaces.append(child)
        roots = [task for task in tasks if task.get("parent_id") is None]
        by_local = {"task-" + quote(task["task_id"].rsplit(":", 1)[-1], safe=""): task["task_id"]
                    for task in tasks if task.get("parent_id")}
        result = []
        for workspace in workspaces:
            for directory, directories, files in os.walk(workspace, followlinks=False):
                directories[:] = sorted(name for name in directories if not name.startswith(".") and (Path(directory) == workspace or name not in PROJECT_CACHE_DIRS)
                                         and not (Path(directory) / name).is_symlink())
                for name in sorted(files):
                    file = Path(directory) / name
                    if name.startswith(".") or not supported_project_file(name, _EXTENSIONS) or file.is_symlink():
                        continue
                    try:
                        info = file.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        continue
                    relative = file.relative_to(path).as_posix()
                    component = file.relative_to(workspace).parts[0]
                    result.append({"path": relative, "name": name, "size": info.st_size,
                                   "task_id": by_local.get(component, roots[0]["task_id"] if len(roots) == 1 else None)})
                    if len(result) >= MAX_ARTIFACTS:
                        return result
        return result

    def _summary(self, path: Path, *, tasks: list[dict] | None = None,
                 events: list[dict] | None = None, artifacts: list[dict] | None = None) -> dict:
        # Keep job metadata and in-process ownership one coherent observation.
        with self._lock:
            return self._summary_locked(path, tasks=tasks, events=events, artifacts=artifacts)

    def _summary_locked(self, path: Path, *, tasks: list[dict] | None = None,
                        events: list[dict] | None = None, artifacts: list[dict] | None = None) -> dict:
        meta = self._meta(path)
        tasks = self._records(path)[0] if tasks is None else tasks
        events = self._events(path)[0] if events is None else events
        artifacts = self._artifacts(path, tasks) if artifacts is None else artifacts
        roots = [task for task in tasks if task.get("parent_id") is None]
        with self._lock:
            owned = path.name in self._active
        states = {task["status"] for task in roots}
        if owned and meta.get("state") in {"queued", "running"}:
            # TaskRecord may already be terminal while the worker finishes the
            # report; keep the job active until all presentation outputs exist.
            status = "queued" if meta.get("state") == "queued" else "running"
        elif meta.get("error_code") == "workspace_delivery":
            status = "cancelled" if meta.get("state") == "cancelled" else "failed"
        elif meta.get("state") == "failed" and meta.get("error_code"):
            status = "failed"
        elif states and states <= _TERMINAL:
            status = "failed" if "failed" in states else "cancelled" if "cancelled" in states else "unverified" if "unverified" in states else "completed"
        elif meta.get("state") == "failed":
            status = "failed"
        elif tasks or meta.get("state") in {"queued", "running"}:
            status = "interrupted"
        else:
            status = "unverified"
        if owned and any(key[0] == path.name and row["answer"] is None for key, row in self._questions.items()):
            status = "waiting_user"
        title = meta.get("title") or (roots[0]["objective"] if roots else None)
        if not title:
            title = next((event.get("goal") or event.get("task") for event in events
                          if isinstance(event.get("goal") or event.get("task"), str)), path.name)
        created = meta.get("created_at") or (roots[0]["created_at"] if roots else _now())
        ended = meta.get("finished_at") or (max(task["updated_at"] for task in roots) if states and states <= _TERMINAL else None)
        end_time = datetime.fromisoformat((ended or _now()).replace("Z", "+00:00"))
        try:
            duration = max(0, (end_time - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() * 1000)
        except ValueError:
            duration = 0
        thread = thread_info(path.name, meta)
        return {"conversation_id": thread["id"] if thread else None,
                "conversation_kind": thread["kind"] if thread else None,
                "recipient": thread["recipient"] if thread else None,
                "id": path.name, "title": title, "mode": meta.get("mode", "imported"),
                "status": status, "created_at": created, "finished_at": ended,
                "duration_ms": None if status == "interrupted" else round(duration, 2),
                "task_count": len(tasks), "completed_count": sum(task["status"] == "completed" for task in tasks),
                "limits": meta.get("request", {}).get("limits", {}),
                "workspace_path": meta.get("workspace_path"),
                "workspace_delivery": meta.get("workspace_delivery"),
                "workspace_member_delivery": meta.get("workspace_member_delivery"),
                "artifact_count": len(artifacts), "error": meta.get("error", ""),
                "error_code": meta.get("error_code", ""),
                "cancel_requested": bool(meta.get("cancel_requested")),
                "scheduler": meta.get("request", {}).get("scheduler", "serial")}

    def list_conversations(self) -> dict:

        conversations = {}
        with self._lock:
            for path in self._directories():
                try:
                    meta = self._meta(path)
                except (StudioError, OSError):
                    continue
                thread = thread_info(path.name, meta)
                if thread is None:
                    continue
                identity = thread["id"]
                item = conversations.setdefault(identity, {
                    "id": identity, "kind": thread["kind"], "members": thread["members"],
                    "title": "", "head_run_id": "", "updated_at": "", "turn_count": 0,
                })
                if path.name == identity:
                    item["title"] = meta.get("title", "")
                if thread["ordinal"] > item["turn_count"]:
                    item.update(head_run_id=path.name, turn_count=thread["ordinal"],
                                updated_at=meta.get("finished_at") or meta.get("created_at", ""))
        ordered = sorted(conversations.values(), key=lambda item: (item["updated_at"], item["id"]), reverse=True)
        return self._redact({"conversations": ordered})

    def list_runs(self) -> dict:
        runs = []
        for path in self._directories()[:200]:
            try:
                runs.append(self._summary(path))
            except (StudioError, OSError, ValueError):
                runs.append({"id": path.name, "title": path.name, "mode": "imported", "status": "failed",
                             "created_at": "", "finished_at": None, "duration_ms": None, "task_count": 0,
                             "completed_count": 0, "artifact_count": 0, "error": "运行记录无法读取；未修改原始数据。"})
        with self._lock:
            active = len(self._active)
        return self._redact({"runs": runs, "active_count": active})

    def detail(self, run_id: str) -> dict:
        with self._lock:
            return self._detail_locked(run_id)

    def _detail_locked(self, run_id: str) -> dict:
        path = self._directory(run_id)
        tasks, acceptance = self._records(path)
        events, log_truncated = self._events(path)
        artifacts = self._artifacts(path, tasks)
        report, report_truncated = _read_file(path, "report.md", MAX_PREVIEW_BYTES, missing=True)
        data = {"run": self._summary(path, tasks=tasks, events=events, artifacts=artifacts),
                "tasks": tasks, "events": events[-MAX_EVENTS:], "events_truncated": log_truncated or len(events) > MAX_EVENTS,
                "artifacts": artifacts, "artifacts_truncated": len(artifacts) >= MAX_ARTIFACTS,
                "acceptance": acceptance, "summary": summarize(events), "summary_incomplete": log_truncated,
                "report": report.decode("utf-8", errors="replace"), "report_truncated": report_truncated}
        data["diagnostics"] = diagnose(tasks, events, acceptance,
                                       active=run_id in self._active, incomplete=log_truncated)
        data["room_events"] = self.room_events(run_id)
        data["pending_questions"] = [{key: row[key] for key in ("request_id", "role", "text")}
                                     for (rid, _), row in self._questions.items()
                                     if rid == run_id and row["answer"] is None]
        data["conversation"] = self._conversation(path)
        return self._redact(data)

    def _conversation(self, path: Path, *, before: int | None = None, with_answers: bool = True) -> dict | None:
        initial = thread_info(path.name, self._meta(path))
        if initial is None:
            return None
        related = []
        for item in self._directories():
            try:
                meta = self._meta(item)
            except StudioError:
                if item == path:
                    raise
                continue
            thread = thread_info(item.name, meta)
            if thread and thread["id"] == initial["id"] and (before is None or thread["ordinal"] < before):
                related.append((thread["ordinal"], item, meta, thread))
        related.sort(key=lambda row: row[0])
        turns, artifact_id, previous = [], None, None
        project_id = initial["id"]
        for index, (ordinal, directory, meta, thread) in enumerate(related, start=1):
            if ordinal != index or previous != thread["parent_run_id"]:
                raise StudioError("会话历史不连续，无法安全继续。", "thread_conflict", 409)
            previous = directory.name
            project_id = knowledge_project(thread, meta.get("workspace_path"), directory.name, project_id)
            answer_text, truncated = "", False
            if with_answers:
                # Keep the selected run and newest response readable. Cap
                # aggregate historic answer previews on each browser poll;
                # selecting an older run retrieves its longer excerpt.
                preview = (MAX_PREVIEW_BYTES if directory == path or index == len(related)
                           else MAX_PREVIEW_BYTES // max(1, len(related) - 2))
                answer, truncated = _read_file(directory, "conversation-answer.txt", preview, missing=True)
                answer_text = answer.decode("utf-8", errors="ignore")
            state = meta.get("state", "interrupted")
            if state in {"running", "queued"} and directory.name not in self._active:
                state = "interrupted"
            if thread.get("inherit_files") is False:
                artifact_id = None
            if thread["intent"] == "edit" and state in {"completed", "unverified"}:
                artifact_id = directory.name
            public_events = self.room_events(directory.name) if with_answers else []
            events = []
            remaining = min(preview, 48_000) if with_answers else 0
            events_truncated = len(public_events) > 24
            for event in reversed(public_events[-24:]):
                if remaining < 1:
                    events_truncated = True
                    break
                excerpt = event["text"][:min(2000, remaining)]
                events_truncated |= len(excerpt) < len(event["text"])
                events.append({**event, "text": excerpt})
                remaining -= len(excerpt) + 128
            attachments = []
            if with_answers and thread["intent"] == "edit" and state in _TERMINAL:
                attachments = [{k: f[k] for k in ("path", "name", "size")}
                               for f in self._artifacts(directory, [])]
                delivered = meta.get("workspace_delivery")
                if delivered and delivered.get("status") == "published":
                    changed = set(delivered.get("files", []))
                    prefix = "agents/" + thread["recipient"] + "/"
                    attachments = [f for f in attachments if f["path"].removeprefix(prefix) in changed]
            turns.append({"artifacts": attachments, "workspace_delivery": meta.get("workspace_delivery"),
                          "workspace_member_delivery": meta.get("workspace_member_delivery"),
                          "recovery_source": thread.get("recovery_source"),
                          "run_id": directory.name, "user": meta.get("title", ""),
                          "events": list(reversed(events)), "events_truncated": events_truncated,
                          "mode": meta.get("mode"),
                          "recipient": thread["recipient"], "answer": answer_text, "answer_truncated": truncated,
                          "intent": thread["intent"], "status": state})
        head = turns[-1] if turns else {}
        return {"id": initial["id"], "kind": initial["kind"], "members": initial["members"],
                "head_run_id": head.get("run_id"), "artifact_run_id": artifact_id,
                "profiles": (related[-1][3] if related else initial).get("profiles", []),
                "saved_roles": sorted(set(initial.get("saved_roles", [])) | set(self.linked_roles(initial["id"]))),
                "knowledge_project_id": project_id,
                "workspace_path": related[-1][2].get("workspace_path") if related else None,
                "can_continue": head.get("status") in _TERMINAL, "turns": turns}

    def _next_turn(self, run_id: str, message: dict) -> dict:
        parent = message.get("parent_run_id")
        if parent is None:
            return {**self.bind_roles(message), "id": run_id, "knowledge_project_id": run_id,
                    "ordinal": 1, "parent_run_id": None, "base_run_id": None}
        room = self._conversation(self._directory(parent), with_answers=False)
        if room is None or room["head_run_id"] != parent or not room["can_continue"]:
            raise StudioError("请等待本轮结束，并从会话的最新一轮发送。", "stale_head", 409)
        if message["recipient"] not in room["members"]:
            raise StudioError("该角色不在会话中。", "non_member", 400)
        base = room["artifact_run_id"] if message.get("inherit_files", True) else None
        if message.get("recover_failed"):
            last = room["turns"][-1]
            if last["status"] != "failed" or last["intent"] != "edit":
                raise StudioError("只有最近一轮修改失败后，才能接续该轮文件。", "invalid_recovery")
            base = parent
        saved_roles = room.get("saved_roles", [])
        profiles = [self.current_role(p["id"]) if p["id"] in saved_roles else p for p in room["profiles"]]
        return {**message, "saved_roles": saved_roles, "id": room["id"], "kind": room["kind"], "members": room["members"],
                "profiles": profiles, "ordinal": len(room["turns"]) + 1, "base_run_id": base,
                "knowledge_project_id": room["knowledge_project_id"] if message.get("inherit_files", True) else run_id}

    def _artifact_data(self, run_id, name, limit):
        root = self._directory(run_id)
        parts = _relative(name)
        segments = parts[4:] if parts[0] == "workspaces" else parts[2:]
        if not supported_project_file(name, _EXTENSIONS) or any(p in PROJECT_CACHE_DIRS for p in segments[:-1]):
            raise StudioError("请选择当前运行生成的产物。", "invalid_path")
        if len(parts) >= 3 and parts[0] == "handoffs" and re.fullmatch(r"[a-f0-9]{32}", parts[1]):
            prefix = "/".join(parts[:2])
            relative = "/".join(parts[2:])
            raw, truncated = _read_file(root, prefix + "/manifest.json", 4 * 1024**2)
            try:
                manifest = {} if truncated else json.loads(raw)
            except (ValueError, UnicodeError):
                manifest = {}
            if not isinstance(manifest, dict) or relative not in manifest:
                raise StudioError("文件不在已发布的成员交接版本中。", "not_found", 404)
            data, truncated = _read_file(root, prefix + "/project/" + relative, 8 * 1024**2)
            if truncated or hashlib.sha256(data).hexdigest() != manifest[relative]:
                raise StudioError("成员交接文件的内容与发布记录不一致。", "handoff_conflict", 409)
            return data[:limit], len(data) > limit
        allowed = (len(parts) >= 3 and parts[0] == "agents"
                   or len(parts) >= 5 and parts[0] == "workspaces" and parts[2] == "agents")
        if not allowed:
            raise StudioError("请选择当前运行生成的产物。", "invalid_path")
        if is_database(name) and any(os.path.lexists(root / (name + suffix)) for suffix in SQLITE_SIDECARS):
            raise StudioError("项目数据库仍在使用。", "database_busy", 409)
        return _read_file(root, name, limit)

    def artifact(self, run_id: str, name: str) -> dict:
        data, truncated = self._artifact_data(run_id, name, MAX_PREVIEW_BYTES)
        if is_png(name):
            return {"path": name, "binary": True, "media_type": "image/png"}
        if is_database(name):
            raise StudioError("SQLite 数据库可下载后打开。", "binary_artifact", 415)
        if b"\x00" in data:
            raise StudioError("该文件不是可预览的文本。", "unsupported_file")
        return self._redact({"path": name, "content": data.decode("utf-8", errors="replace"), "truncated": truncated})

    def download_artifact(self, run_id, name):
        data, truncated = self._artifact_data(run_id, name, 8 * 1024**2)
        if truncated:
            raise StudioError("下载文件超过 8 MiB。", "bundle_too_large", 413)
        try:
            validate_project_data(name, data, secret=self.settings.api_key)
        except ValueError as exc:
            raise StudioError(str(exc), "unsupported_file", 415) from None
        return data if is_database(name) or is_png(name) else self._redact(data.decode()).encode()

    def artifact_bundle(self, run_id: str) -> bytes:
        """Download finished workspace text/code, preserving relative imports."""
        with self._lock:
            path = self._directory(run_id)
            if run_id in self._active:
                raise StudioError("任务结束后可下载完整产物；运行时可逐个预览。", "run_active", 409)
            tasks, _ = self._records(path)
            artifacts = self._artifacts(path, tasks)
            if not artifacts:
                raise StudioError("这次任务尚未生成可下载的产物。", "not_found", 404)
            if len(artifacts) >= MAX_ARTIFACTS:
                raise StudioError("产物数超过下载范围，请从本机工作区读取。", "bundle_too_large", 413)
            meta = self._meta(path)
            single = "thread" in meta or meta.get("mode") in {"demo", "single"}
            output, total, names = io.BytesIO(), 0, set()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                for artifact in artifacts:
                    data, truncated = _read_file(path, artifact["path"], 8 * 1024 * 1024)
                    if truncated or total + len(data) > 20 * 1024 * 1024:
                        raise StudioError("产物超过单文件 8 MiB 或总计 20 MiB 下载范围，请从本机工作区读取。", "bundle_too_large", 413)
                    artifact_path = artifact["path"]
                    if is_database(artifact_path) and any(os.path.lexists(path / (artifact_path + suffix))
                                                          for suffix in SQLITE_SIDECARS):
                        raise StudioError("数据库仍有日志文件，请关闭并完成 checkpoint 后下载。", "database_busy", 409)
                    try:
                        validate_project_data(artifact_path, data, secret=self.settings.api_key)
                    except ValueError as exc:
                        raise StudioError(str(exc), "unsupported_file", 415) from None
                    content = data if is_database(artifact_path) or is_png(artifact_path) else self._redact(data.decode("utf-8"))
                    parts = PurePosixPath(artifact["path"]).parts
                    index = parts.index("agents") + (2 if single else 1)
                    name = "/".join(parts[index:])
                    _relative(name)
                    if name in names:
                        raise StudioError("产物相对路径重复，未生成不完整压缩包。", "bundle_conflict", 409)
                    names.add(name)
                    total += len(data)
                    archive.writestr(name, content)
            return output.getvalue()

    def cancel(self, run_id: str) -> dict:
        with self._lock:
            path = self._directory(run_id)
            if run_id not in self._active:
                return self._redact(self._summary(path))
            self._update_meta(run_id, cancel_requested=True)
            self._cancellations[run_id].set()
            self._answers.notify_all()
            return self._redact(self._summary(path))

    def _request(self, payload: Any) -> dict:
        if not isinstance(payload, dict) or set(payload) - {"request_id", "mode", "objective", "scheduler", "thinking", "acceptance_files", "limits", "conversation", "acceptance_checks", "child_checks", "workspace_path"}:
            raise StudioError("任务参数无效。")
        identity = payload.get("request_id")
        try:
            if type(identity) is not str or len(identity) > 64:
                raise ValueError()
            uuid.UUID(identity)
        except (ValueError, AttributeError):
            raise StudioError("request_id 必须是 UUID。") from None
        mode = payload.get("mode", "demo")
        if type(mode) is not str or mode not in {item["id"] for item in _MODES}:
            raise StudioError("执行模式无效。")
        objective = payload.get("objective", "")
        if type(objective) is not str or len(objective) > 10000:
            raise StudioError("任务描述必须是不超过 10000 字符的文本。")
        objective = _DEMO_TITLES.get(mode, objective.strip())
        if not objective:
            raise StudioError("请填写任务描述。")
        scheduler = payload.get("scheduler", "serial")
        if type(scheduler) is not str or scheduler not in {"serial", "parallel", "evented"} or (mode in {"demo", "team-demo"} and scheduler != "serial"):
            raise StudioError("离线演示仅支持串行调度；调度方式须为 serial/parallel/evented。")
        thinking = payload.get("thinking", self.settings.thinking)
        if type(thinking) is not str or thinking not in {"disabled", "enabled"}:
            raise StudioError("thinking 必须为 disabled 或 enabled。")
        files = payload.get("acceptance_files", [])
        if not isinstance(files, list) or len(files) > 20 or any(type(name) is not str for name in files):
            raise StudioError("验收文件必须是最多 20 个相对路径。")
        for name in files:
            _relative(name)
        if len(files) != len(set(files)):
            raise StudioError("验收文件不可重复。")
        if mode in {"demo", "team-demo"}:
            files = []
        checks = payload.get("acceptance_checks", [])
        child_checks = payload.get("child_checks", {})
        if (not isinstance(checks, list) or not isinstance(child_checks, dict) or len(child_checks) > 20
                or (mode in {"demo", "team-demo"} and (checks or child_checks))
                or (child_checks and mode != "team")):
            raise StudioError("验收规则或子任务验收配置无效。")
        try:
            build_checks(files, checks)
            for child, spec in child_checks.items():
                if (not isinstance(child, str) or not _ID.fullmatch(child)
                        or not isinstance(spec, dict) or spec.keys() - {"files", "checks"}
                        or not (spec.get("files") or spec.get("checks"))):
                    raise ValueError("子任务验收应绑定计划内的有效 ID。")
                child_files = spec.get("files", [])
                if not isinstance(child_files, list) or len(child_files) > 20:
                    raise ValueError("子任务验收文件最多 20 个。")
                for filename in child_files:
                    _relative(filename)
                if len(set(child_files)) != len(child_files):
                    raise ValueError("子任务验收文件重复。")
                build_checks(child_files, spec.get("checks", []))
        except ValueError as exc:
            raise StudioError(str(exc)) from None
        limits = payload.get("limits", {})
        try:
            if not isinstance(limits, dict):
                raise ValueError()
            configured_limits(self.settings.limits, limits)
        except (ValueError, TypeError):
            raise StudioError("预算无效：轮数 1–1000、累计 token 0–100000000（0 表示不限）、时限 1–86400 秒。") from None
        result = {"request_id": identity, "mode": mode, "objective": objective,
                  "scheduler": scheduler, "thinking": thinking, "acceptance_files": files}
        if "workspace_path" in payload:
            if mode == "team-demo" or mode == "team" and "conversation" not in payload:
                raise StudioError("请选择私聊或群聊来绑定本地工作区。")
            result["workspace_path"] = payload["workspace_path"]
            if not isinstance(result["workspace_path"], str):
                raise StudioError("工作区路径须为文本。")
        # Preserve the fingerprint of old requests with no budget overrides.
        if limits:
            result["limits"] = limits
        if checks:
            result["acceptance_checks"] = checks
        if child_checks:
            result["child_checks"] = child_checks
        if "conversation" in payload:
            if mode not in {"single", "team"} or scheduler != "serial":
                raise StudioError("会话支持定向回复或群内协作，请使用串行调度。")
            try:
                message = self.validate_role_refs(payload["conversation"])
            except ValueError as exc:
                raise StudioError(str(exc)) from None
            if mode == "team" and (message["intent"] != "edit" or message.get("kind", "group") != "group"):
                raise StudioError("群内协作需要向群聊发送任务。")
            if message["intent"] == "question" and (files or checks):
                raise StudioError("只读问答无需填写验收文件。")
            result["conversation"] = message
        return result

    @contextmanager
    def _admission(self):
        # Serialize replay checks and parent selection across Studio processes.
        # The actual state remains in atomically published run directories.
        database = self.runs_dir / ".studio-admission.sqlite3"
        for name in (database, database.with_name(database.name + "-journal")):
            if name.is_symlink():
                raise StudioError("会话锁文件无效。", "storage_error", 409)
        connection = sqlite3.connect(database, timeout=10)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield
        finally:
            connection.close()


    def start(self, payload: Any) -> dict:
        request = self._request(payload)
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self._lock, self._admission():
            if self._closed:
                raise StudioError("工作室正在关闭。", "unavailable", 503)
            for path in self._directories():
                try:
                    meta = self._meta(path)
                except StudioError:
                    continue
                if meta.get("request_id") == request["request_id"]:
                    if meta.get("fingerprint") != fingerprint:
                        raise StudioError("同一次提交不能改成不同的任务。", "conflict", 409)
                    return self._redact(self._summary(path))
            if request["mode"] in {"single", "team"} and not self.settings.api_key.strip():
                raise StudioError("本机尚未配置 DeepSeek API key，请先配置后启动工作室。", "provider_unavailable", 409)
            run_id = "studio-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:12]
            thread = self._next_turn(run_id, request["conversation"]) if "conversation" in request else None
            if thread and request["mode"] == "team" and thread["kind"] != "group":
                raise StudioError("私聊内不能启动群组协作。")
            if thread and request.get("child_checks", {}).keys() - (set(thread["members"]) - {thread["recipient"]}):
                raise StudioError("群内子任务验收须绑定给当前负责人以外的成员。")
            if not self._slots.acquire(blocking=False):
                raise StudioError("同时运行的任务已达上限，请等待当前任务结束。", "busy", 429)
            path = self.runs_dir / run_id
            staged = None
            published = False
            try:
                staged = Path(tempfile.mkdtemp(prefix=".studio-pending-", dir=self.runs_dir))
                meta = {"schema_version": 1, "request_id": request["request_id"], "fingerprint": fingerprint,
                        "request": request, "title": request["objective"], "mode": request["mode"],
                        "state": "queued", "created_at": _now()}
                assignee = "orchestrator" if request["mode"] in {"team", "team-demo"} else "worker"
                local = request.get("workspace_path")
                previous_local = None
                if thread and thread["parent_run_id"] and thread.get("inherit_files", True):
                    previous_local = self._meta(self._directory(thread["parent_run_id"])).get("workspace_path")
                    local = local if local is not None else previous_local
                if local is not None:
                    try:
                        local = studio_workspace.select_root(local, self.runs_dir)
                        if previous_local and previous_local != local:
                            raise StudioError("本会话已绑定工作区，请选择新项目以切换文件夹。")
                        active_thread = thread or thread_info(run_id, meta)
                        baseline = studio_workspace.checkout(local, self._workspace(staged, active_thread),
                                                             _EXTENSIONS, self.settings.api_key)
                    except StudioError:
                        raise
                    except (OSError, ValueError) as exc:
                        raise StudioError(str(exc) if isinstance(exc, ValueError) else "无法读取所选工作区。",
                                          "workspace_unavailable", 409) from None
                    meta["workspace_path"] = local
                    _write_json(staged, "workspace-baseline.json", baseline)
                if thread:
                    thread["knowledge_project_id"] = knowledge_project(thread, local, run_id)
                    meta["thread"] = thread
                    assignee = thread["recipient"]
                    if thread["base_run_id"] and (local is None or thread.get("recover_failed")):
                        if local and thread.get("recover_failed"):
                            old_root = self._directory(thread["base_run_id"])
                            if self._projection_basis(old_root) != baseline:
                                raise StudioError("上次执行后本地文件已改变，请从当前工作区继续。",
                                                  "workspace_conflict", 409)
                            shutil.rmtree(self._workspace(staged, thread))
                        source = self._directory(thread["base_run_id"])
                        author = thread_info(source.name, self._meta(source))["recipient"]
                        if thread.get("recover_failed") and not any((source / "agents" / author).glob("*")):
                            candidate = self._failed_room_checkpoint(source)
                            if candidate:
                                source, author, task_id = candidate
                                thread["recovery_source"] = {"role": author, "task_id": task_id}
                        try:
                            copy_project(source, author, self._workspace(staged, thread), _EXTENSIONS,
                                         secret=self.settings.api_key)
                        except SnapshotError as exc:
                            raise StudioError(str(exc), "unsupported_snapshot", 409) from None
                _write_json(staged, "studio.json", meta)
                service = TaskService(TaskStore(staged / "tasks.sqlite3"))
                service.create(task_id=run_id, creation_key=("orchestration:" if request["mode"] in {"team", "team-demo"} and not thread else "root:") + run_id,
                               objective=request["objective"], owner=assignee, assignee=assignee)
                staged.rename(path)
                published = True
                self._active.add(run_id)
                self._cancellations[run_id] = threading.Event()
                result = self._summary(path)
                self._executor.submit(self._worker, run_id, request)
                return self._redact(result)
            except BaseException:
                self._active.discard(run_id)
                self._cancellations.pop(run_id, None)
                self._slots.release()
                if published:
                    self._update_meta(run_id, state="failed", finished_at=_now(), error="工作线程未能启动。")
                raise
            finally:
                if staged and staged.exists():
                    shutil.rmtree(staged)

    def _failed_room_checkpoint(self, path):
        # Only the explicit recover_failed branch reaches here. A checkpoint is
        # a host-validated project snapshot, separate from the member's private
        # execution directory. Never choose between unrelated drafts implicitly.
        events = self.room_events(path.name)
        reports = [r for r in events if r["kind"] == "member_report"]
        if not reports or any(r.get("published") for r in reports) or len({r["role"] for r in reports}) != 1:
            return None
        row = reports[-1]
        store = TaskStore(path / "tasks.sqlite3")
        task = store.get(row["task_id"])
        if task.parent_id != path.name or task.assignee_profile_id != row["role"]:
            return None
        attempts = store.attempts(task.task_id)
        if not attempts or not attempts[-1].context_ref:
            return None
        ctx = store.get_blob(attempts[-1].context_ref)["context"]
        workspace = Path(ctx.get("workspace", ""))
        assignment = workspace.parent.parent
        checkpoint = assignment / "checkpoint"
        if (assignment.parent != path / "assignments" or not assignment.name.startswith("member-")
                or workspace != assignment / "agents" / row["role"]
                or ctx.get("public_room") != thread_info(path.name, self._meta(path))["id"]
                or any(p.is_symlink() for p in (assignment.parent, assignment, checkpoint))
                or not (checkpoint / "agents" / row["role"]).is_dir()):
            return None
        return checkpoint, row["role"], task.task_id

    @staticmethod
    def _workspace(path: Path, thread: dict) -> Path:
        if thread["intent"] == "question":
            return path / "context" / "project"
        return path / "agents" / thread["recipient"]

    def _projection_basis(self, path, *, initial=False):
        names = ["workspace-baseline.json"] if initial else ["workspace-projection.json", "workspace-baseline.json"]
        for name in names:
            raw, clipped = _read_file(path, name, 4 * 1024**2, missing=True)
            if clipped:
                raise StudioError("工作区版本清单超过上限。", "workspace_conflict", 409)
            if raw or (path / name).exists():
                try:
                    data = json.loads(raw)
                    studio_workspace._baseline_files(data, _EXTENSIONS)
                    identity = data["directory"]
                    if (not isinstance(identity, list) or len(identity) != 2
                            or any(type(i) is not int for i in identity)):
                        raise ValueError()
                    return data
                except (KeyError, TypeError, ValueError):
                    raise StudioError("工作区版本清单无效。", "workspace_conflict", 409) from None
        raise StudioError("本轮缺少原始工作区版本清单。", "workspace_conflict", 409)

    def _project_workspace(self, path, local, project, *, require_match=False, final=False):
        """Caller holds the Studio admission lock; the initial checkout is immutable."""
        initial = self._projection_basis(path, initial=True)
        previous = self._projection_basis(path)
        if previous["directory"] != initial["directory"]:
            raise studio_workspace.WorkspaceError("所选项目文件夹身份已变化。")
        verify_paths = set()
        if final:
            # A member's receipt records what was true when it published.
            # Recheck this run's own files (including earlier deletions) at
            # final handoff, even if the lead's private copy has no new delta.
            verify_paths.update(name for name in initial["files"].keys() | previous["files"].keys()
                                if initial["files"].get(name) != previous["files"].get(name))
            for event in self.room_events(path.name):
                receipt = event.get("workspace_delivery") or {}
                if event["kind"] == "member_report" and event.get("published") and receipt.get("status") == "published":
                    verify_paths.update(receipt["files"])
        delta, basis = studio_workspace.publish(local, project, previous, _EXTENSIONS, self.settings.api_key,
                                               return_basis=True, require_match=require_match, verify_paths=verify_paths)
        _write_json(path, "workspace-projection.json", basis)
        original, current = initial["files"], basis["files"]
        changed = sorted(k for k in original.keys() | current.keys() if original.get(k) != current.get(k))
        return delta, changed

    @staticmethod
    def _projection_error(exc):
        return str(exc) if isinstance(exc, ValueError) else "写入本地工作区失败，公共版本仍保存在聊天中。"

    def _record_crew_event(self, run_id, role, kind, text, **fields):
        # RoomCrew emits this event after its validated snapshot replaces the
        # lead's public project. Private drafts and other events never write back.
        if kind == "member_report" and fields.get("published") is True:
            path = self._directory(run_id)
            with self._lock, self._admission():
                meta = self._meta(path)
                thread = thread_info(run_id, meta)
                if meta.get("workspace_path") and thread["intent"] == "edit":
                    try:
                        if self._cancellations[run_id].is_set():
                            receipt = {"status": "cancelled", "files": []}
                        else:
                            delta, _ = self._project_workspace(path, meta["workspace_path"], self._workspace(path, thread))
                            receipt = {"status": "published", "files": delta}
                    except Exception as exc:
                        receipt = {"status": "failed", "message": self._projection_error(exc)}
                    receipt.update(source="member", role=role, task_id=fields.get("task_id"))
                    fields["workspace_delivery"] = receipt
                    self._update_meta(run_id, workspace_member_delivery=receipt)
        return self._record_room_event(run_id, role, kind, text, **fields)

    def deliver_public_workspace(self, run_id):
        """Deliver an existing terminal group's canonical project without changing its grading."""
        with self._lock, self._admission():
            path = self._directory(run_id)
            meta = self._meta(path)
            thread = thread_info(run_id, meta)
            if (run_id in self._active or meta.get("state") not in {"completed", "failed", "unverified"}
                    or not thread or thread["kind"] != "group" or thread["intent"] != "edit"
                    or not meta.get("workspace_path")):
                raise StudioError("需要已结束且绑定本地工作区的群任务。", "workspace_unavailable", 409)
            reports = [e for e in self.room_events(run_id) if e["kind"] == "member_report" and e.get("published") is True]
            if not reports:
                raise StudioError("该轮没有已发布的成员交接。", "missing_publication", 409)
            latest = reports[-1]
            try:
                # Use the same full snapshot validation as the crew before
                # exposing an archived canonical directory to the local user.
                with tempfile.TemporaryDirectory(dir=path, prefix=".public-delivery-") as staged:
                    project = Path(staged) / "project"
                    copy_project(path, thread["recipient"], project, _EXTENSIONS, secret=self.settings.api_key)
                    _, changed = self._project_workspace(path, meta["workspace_path"], project, require_match=True)
            except (OSError, ValueError) as exc:
                raise StudioError(self._projection_error(exc), "workspace_conflict", 409) from None
            receipt = {"status": "published", "files": changed, "source": "public_run",
                       "role": latest["role"], "task_id": latest.get("task_id")}
            self._update_meta(run_id, workspace_member_delivery=receipt)
            return receipt

    def _worker(self, run_id: str, request: dict) -> None:
        outcome = {"state": "failed", "error": "执行发生错误，请查看任务记录与日志。"}
        try:
            self._update_meta(run_id, state="running")
            outcome = self._execute(run_id, request)
            path = self._directory(run_id)
            meta = self._meta(path)
            thread = thread_info(run_id, meta)
            if meta.get("workspace_path") and thread["intent"] == "edit" and outcome["state"] in {"completed", "unverified"}:
                try:
                    with self._lock, self._admission():
                        if self._cancellations[run_id].is_set():
                            outcome = {"state": "cancelled", "error_code": "workspace_delivery",
                                       "error": "已取消写回本地工作区。",
                                       "workspace_delivery": {"status": "cancelled", "files": []}}
                        else:
                            _, files = self._project_workspace(path, meta["workspace_path"],
                                                              self._workspace(path, thread), final=True)
                            outcome["workspace_delivery"] = {"status": "published", "files": files}
                except Exception as exc:
                    msg = str(exc) if isinstance(exc, ValueError) else "写入本地工作区失败，本轮产物保存在聊天中。"
                    outcome = {"state": "failed", "error_code": "workspace_delivery", "error": msg,
                               "workspace_delivery": {"status": "failed", "message": msg}}
        except Exception as exc:
            # Only an exception category is exposed; messages may contain URLs,
            # credentials or arbitrary tool output.
            outcome = {"state": "failed", "error_code": type(exc).__name__,
                       "error": "执行发生错误，请查看任务记录与日志。"}
            try:
                with Tracer(self._directory(run_id)) as tracer:
                    tracer.event("studio_error", task_id=run_id, error_code=type(exc).__name__,
                                 message=outcome["error"],
                                 frames=[{"file": Path(frame.filename).name, "function": frame.name,
                                          "line": frame.lineno}
                                         for frame in traceback.extract_tb(exc.__traceback__)[-8:]])
            except Exception:
                pass  # A storage failure can prevent even the error journal.
        finally:
            with self._lock:
                try:
                    self._update_meta(run_id, finished_at=_now(), **outcome)
                finally:
                    self._active.discard(run_id)
                    self._cancellations.pop(run_id, None)
                    self._slots.release()

    def _execute(self, run_id: str, request: dict) -> dict:
        # Import the host-owned demo fixtures lazily to avoid the CLI entrypoint's
        # optional Studio import becoming a module cycle.
        from .cli import DEMO_SCRIPT, _load_orchestration_acceptance
        path = self._directory(run_id)
        settings = replace(self.settings, thinking=request["thinking"],
                           limits=configured_limits(self.settings.limits, request.get("limits", {})))
        should_cancel = self._cancellations[run_id].is_set
        mode = request["mode"]
        examples = Path(__file__).resolve().parent.parent / "examples"
        if mode == "demo":
            provider = ScriptedProvider(DEMO_SCRIPT)
        elif mode == "team-demo":
            provider = ScriptedProvider.from_json(examples / "mock_plan.json")
        else:
            provider = DeepSeekProvider(api_key=settings.api_key, base_url=settings.base_url, model=settings.model,
                                        thinking=settings.thinking, reasoning_effort=settings.reasoning_effort,
                                        max_output_tokens=settings.max_output_tokens, timeout=settings.request_timeout_seconds,
                                        deadline_seconds=settings.request_deadline_seconds, max_retries=settings.max_retries)
        service = TaskService(TaskStore(path / "tasks.sqlite3"))
        with Tracer(path) as tracer:
            tracer.event("studio_configuration", task_id=run_id,
                         configuration={"model": settings.model, "thinking": settings.thinking,
                                        "max_output_tokens": settings.max_output_tokens,
                                        "request_timeout_seconds": settings.request_timeout_seconds,
                                        "request_deadline_seconds": settings.request_deadline_seconds,
                                        "max_retries": settings.max_retries,
                                        "max_steps": settings.limits.max_steps,
                                        "max_task_seconds": settings.limits.max_seconds,
                                        "max_total_tokens": settings.limits.max_total_tokens,
                                        "context_window_tokens": settings.context_window_tokens,
                                        "max_output_recovery_attempts": settings.max_output_recovery_attempts,
                                        "mode": mode, "scheduler": request["scheduler"]})
            checks = build_checks(request["acceptance_files"], request.get("acceptance_checks", []))
            if mode in {"team", "team-demo"} and "conversation" not in request:
                root_acceptance, children = (_load_orchestration_acceptance(str(examples / "acceptance-plan.json"))
                                             if mode == "team-demo" else (checks, {
                                                 child: build_checks(spec.get("files", []), spec.get("checks", []))
                                                 for child, spec in request.get("child_checks", {}).items()
                                             }))
                orchestrator = Orchestrator(provider=provider, settings=settings, run_dir=path, tracer=tracer,
                                            task_service=service, root_acceptance=root_acceptance, acceptances=children,
                                            should_cancel=should_cancel)
                result = orchestrator.run(request["objective"], scheduler=request["scheduler"],
                                          max_parallel=2, goal_id=run_id)
            else:
                tracer.event("case_registered", schema_version=2, root_id=run_id, task_id=run_id,
                             level="goal", task=request["objective"])
                metadata = self._meta(path)
                thread = thread_info(run_id, metadata)
                roles = RoleSession(self.role_memory, thread, metadata.get("workspace_path"), run_id, settings.api_key)
                recipient = thread["recipient"]
                question = thread["intent"] == "question"
                context = ""
                if "conversation" in request:
                    room = self._conversation(path, before=thread["ordinal"])
                    context = project_context(thread, room["turns"])
                    receipt = roles.remember_instruction(thread, request["objective"])
                    if receipt:
                        context += "\n" + receipt
                        tracer.event("role_memory_receipt", task_id=run_id, role=recipient, message=receipt)
                    knowledge = roles.context(recipient, request["objective"])
                    if knowledge:
                        context += "\n" + knowledge
                if mode != "demo" and checks:
                    delivery = {"files": request["acceptance_files"], "checks": request.get("acceptance_checks", [])}
                    context += "\n用户指定的交付检查：" + json.dumps(delivery, ensure_ascii=False)
                acceptance = accept_files("notes.md") if mode == "demo" else checks
                workspace = self._workspace(path, thread)
                sandbox = Sandbox(workspace, agent_name=recipient)
                crew = None
                if mode == "team":
                    crew = RoomCrew(run_id, path, thread, room["turns"], provider, settings, tracer,
                                    service, should_cancel, self._human_handler, self._record_crew_event,
                                    self.room_events, _EXTENSIONS, request.get("child_checks", {}),
                                    {"files": request["acceptance_files"], "checks": request.get("acceptance_checks", [])},
                                    role_session=roles)
                    context += "\n" + crew.instructions()
                    self._record_room_event(run_id, "user", "goal", request["objective"], recipient=recipient, task_id=run_id)
                registry = roles.registry(recipient, build_default_registry(read_only=question), read_only=question)
                agent = Agent(name=recipient, provider=provider, registry=registry,
                              sandbox=sandbox, settings=settings, tracer=tracer, task_service=service,
                              human_handler=(self._human_handler(run_id, recipient, deadline=crew.deadline - crew.close_seconds)
                                             if crew else self._human_handler(run_id, recipient)
                                             if "conversation" in request else None),
                              delegate_handler=crew.delegate if crew else None,
                              human_request_gate=crew.human_request_gate if crew else None, should_cancel=should_cancel)
                if question:
                    agent.unit.enable_control_tools = False
                result = agent.run(request["objective"], task_id=run_id, acceptance=acceptance,
                                   budget=crew.budget if crew else None, conversation_context=context,
                                   system_prompt=COORDINATOR_PROMPT if crew else None,
                                   before_answer=crew.before_answer if crew else None,
                                   step_policy=crew.coordinator_policy if crew else None,
                                   on_step=crew.observe_coordinator if crew else None)
                if result.answer:
                    # Write the public final answer only. Keep reasoning, raw
                    # tool buffers, and credentials out of the transcript.
                    answer_file = path / "conversation-answer.txt"
                    temporary = path / (".reply-" + uuid.uuid4().hex)
                    temporary.write_text(self._redact(result.answer), encoding="utf-8")
                    temporary.replace(answer_file)
        render_report(path)
        status = service.store.get(run_id).status
        return {"state": status if status in _TERMINAL else "failed",
                "error": "" if status in {"completed", "unverified"} else
                "用户请求停止" if status == "cancelled" else "任务未通过验收或执行未正常结束。"}
