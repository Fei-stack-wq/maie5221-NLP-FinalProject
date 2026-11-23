"""Shared team project and private agent workspaces.

Isolation model (course requirement 2.2): every agent works in its own
directory and can only touch files there (``Sandbox``). Collaboration happens
through one explicit channel — the team *project*:

* ``checkout(workspace)`` mirrors the current project into an agent's private
  workspace before it starts an assignment;
* ``publish(workspace, baseline)`` copies what that agent created, changed or
  deleted back into the project when the assignment ends.

Assignments run one at a time, so a publish never races another agent. If the
project nevertheless changed under an agent (e.g. the lead edited the same
file), the agent's version wins and the conflict is reported, never hidden.

Unlike the previous implementation there are no review gates, credential
heuristics or file-type allow-lists on publishing: whatever the agent produced
is what the team sees. Only caches, VCS metadata and oversized files are
skipped, and every skip is reported.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .project_data import PROJECT_CACHE_DIRS

SKIP_DIRS = PROJECT_CACHE_DIRS | {".git", ".hg", ".svn", ".idea", ".vscode"}
SKIP_FILES = {".DS_Store", "Thumbs.db"}
MAX_FILE_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class Change:
    path: str
    change: str  # added | modified | deleted
    sha256: str = ""
    size: int = 0
    conflict: bool = False

    def as_dict(self) -> dict:
        data = {"path": self.path, "change": self.change}
        if self.sha256:
            data.update(sha256=self.sha256, size=self.size)
        if self.conflict:
            data["conflict"] = True
        return data


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def scan(root: Path) -> tuple[dict[str, str], list[str]]:
    """Return ({relative_path: sha256}, skipped) for regular files under ``root``."""
    files: dict[str, str] = {}
    skipped: list[str] = []
    if not root.exists():
        return files, skipped
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not Path(folder, d).is_symlink())
        for name in sorted(names):
            if name in SKIP_FILES:
                continue
            path = Path(folder, name)
            rel = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file():
                skipped.append(rel + " (link or special file)")
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                skipped.append(rel + f" (larger than {MAX_FILE_BYTES // (1024 * 1024)} MB)")
                continue
            files[rel] = _digest(path)
    return files, skipped


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    temporary = dst.with_name(f".{dst.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    shutil.copy2(src, temporary)
    os.replace(temporary, dst)


def _remove(path: Path, stop: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    parent = path.parent
    while parent != stop and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


class Project:
    """The team's published file tree plus an append-only change log."""

    def __init__(self, root: Path, log_path: Path) -> None:
        self.root = root
        self.log_path = log_path
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def manifest(self) -> dict[str, str]:
        with self._lock:
            return scan(self.root)[0]

    def files(self) -> list[dict]:
        with self._lock:
            files, _ = scan(self.root)
            return [{"path": rel, "size": (self.root / rel).stat().st_size, "sha256": sha}
                    for rel, sha in sorted(files.items())]

    def checkout(self, workspace: Path) -> dict[str, str]:
        """Make ``workspace`` equal to the project; return the baseline manifest."""
        with self._lock:
            workspace.mkdir(parents=True, exist_ok=True)
            project, _ = scan(self.root)
            local, _ = scan(workspace)
            for rel, sha in project.items():
                if local.get(rel) != sha:
                    _copy(self.root / rel, workspace / rel)
            for rel in local.keys() - project.keys():
                _remove(workspace / rel, workspace)
            return dict(project)

    def publish(self, workspace: Path, baseline: dict[str, str], *, author: str, task_id: str,
                note: str = "") -> tuple[list[Change], list[str]]:
        """Apply the workspace's changes since ``baseline``; return (changes, skipped)."""
        with self._lock:
            local, skipped = scan(workspace)
            project, _ = scan(self.root)
            changes: list[Change] = []
            for rel in sorted(local.keys() | baseline.keys()):
                before, after = baseline.get(rel), local.get(rel)
                if before == after:
                    continue
                conflict = project.get(rel) != before
                if after is None:
                    if rel in project:
                        _remove(self.root / rel, self.root)
                    changes.append(Change(rel, "deleted", conflict=conflict))
                else:
                    _copy(workspace / rel, self.root / rel)
                    kind = "added" if before is None else "modified"
                    changes.append(Change(rel, kind, after, (workspace / rel).stat().st_size, conflict))
            if changes:
                entry = {"time": time.time(), "author": author, "task_id": task_id, "note": note[:500],
                         "project_scope":os.path.relpath(self.root, self.log_path.parent),
                         "changes": [c.as_dict() for c in changes]}
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return changes, skipped

    def history(self, limit: int = 200) -> list[dict]:
        with self._lock:
            if not self.log_path.exists():
                return []
            lines = self.log_path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in lines if line.strip()]

    def resolve(self, rel: str) -> Path:
        """Resolve a project-relative path for reading; refuse escapes."""
        candidate = (self.root / rel).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("path outside the project")
        return candidate


def listing(files: list[dict], limit: int = 120) -> str:
    """Compact project listing for prompts."""
    if not files:
        return "(the team project is empty)"
    rows = [f"- {f['path']} ({f['size']} bytes)" for f in files[:limit]]
    if len(files) > limit:
        rows.append(f"- … and {len(files) - limit} more files")
    return "\n".join(rows)
