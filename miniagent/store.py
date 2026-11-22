"""Durable state for groups, their public chat and their tasks (plain JSON files).

Layout under ``runs_dir``::

    groups/<group_id>/group.json            members, lead, mode
    groups/<group_id>/chat.jsonl            public timeline, one event per line, with ``seq``
    groups/<group_id>/project/              the team project (published files)
    groups/<group_id>/project-log.jsonl     who published what, per assignment
    groups/<group_id>/agents/<id>/workspace private sandbox of one agent
    groups/<group_id>/agents/<id>/memory.md long-term notes written by the agent
    groups/<group_id>/agents/<id>/journal.jsonl  the agent's past assignments
    groups/<group_id>/tasks/<task_id>/task.json  status and metrics of one task
    groups/<group_id>/tasks/<task_id>/trace.jsonl every agent event of that task
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .chat_stream import ChatStream
from .workspace import Project

_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,47}\Z")


def now() -> float:
    return round(time.time(), 3)


@dataclass
class Member:
    id: str
    name: str
    prompt: str
    note: str = ""

    def validate(self) -> None:
        if not _ID.fullmatch(self.id or ""):
            raise ValueError(f"invalid member id {self.id!r}: use letters, digits, _ or -, starting with a letter")
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 100:
            raise ValueError(f"member {self.id} needs a name (max 100 characters)")
        if not isinstance(self.note,str) or len(self.note)>3000:
            raise ValueError("Invalid group note")
        if not isinstance(self.prompt, str) or len(self.prompt) > 8000:
            raise ValueError(f"member {self.id} prompt is longer than 8000 characters")


@dataclass
class Group:
    id: str
    name: str
    members: list[Member]
    lead: str
    mode: str = "interactive"
    created_at: float = field(default_factory=now)

    kind: str = "group"
    pinned: bool = False
    archived: bool = False
    folder: str = ""
    role_ids: list[str] = field(default_factory=list)
    created_by: dict | None = None

    def validate(self) -> None:
        if self.kind not in {"group", "dm", "assistant"} or (self.kind != "group" and len(self.members) != 1):
            raise ValueError("DM and assistant conversations have exactly one member")
        if not self.name.strip() or len(self.name) > 120:
            raise ValueError("group name is required (max 120 characters)")
        if not 1 <= len(self.members) <= 12:
            raise ValueError("a group has 1 to 12 members")
        ids = [m.id for m in self.members]
        if len({i.lower() for i in ids}) != len(ids):
            raise ValueError("member ids must be unique")
        for member in self.members:
            member.validate()
        if self.lead not in ids:
            raise ValueError("the lead must be one of the members")
        if self.mode not in {"interactive", "autonomous"}:
            raise ValueError("mode must be interactive or autonomous")

    def member(self, member_id: str) -> Member:
        for member in self.members:
            if member.id == member_id:
                return member
        raise KeyError(member_id)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Group":
        return cls(id=data["id"], name=data["name"], lead=data["lead"], mode=data.get("mode", "interactive"),
                   created_at=data.get("created_at", now()), kind=data.get("kind", "group"),
                   pinned=data.get("pinned", False), archived=data.get("archived", False),
                   folder=data.get("folder", ""), role_ids=data.get("role_ids", []), created_by=data.get("created_by"),
                   members=[Member(**m) for m in data["members"]])


class GroupStore:
    """One group's files. Appends to the chat are serialized and numbered."""

    def __init__(self, root: Path, group: Group) -> None:
        self.root = root
        self.group = group
        self._lock = threading.RLock()
        self.turn_lock = threading.Lock()
        self._notice_ids: set[str] = set()
        self._seq = 0
        self._chat_path = root / "chat.jsonl"
        if self._chat_path.exists():
            for line in self._chat_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    event = json.loads(line)
                    self._seq = max(self._seq, event.get("seq", 0))
                    if event.get("notice_id"):
                        self._notice_ids.add(event["notice_id"])
        self.previews = ChatStream(self._seq)
        self.select_project()

    def select_project(self):
        # Every binding has its own checkout and sync baseline. Keep the existing
        # shared checkout for a folder synchronized by an older Studio.
        home = self.root
        folder = self.group.folder
        if folder:
            legacy = self.root / "folder-sync.json"
            if not legacy.is_file() or json.loads(legacy.read_bytes()).get("folder") != folder:
                home = self.root / "workspaces" / hashlib.sha256(folder.encode("utf-8")).hexdigest()
        self.project = Project(home / "project", self.root / "project-log.jsonl")
        self.folder_state = home / "folder-sync.json"

    # -- chat -------------------------------------------------------------
    def append(self, kind: str, **fields: Any) -> dict:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "time": now(), "kind": kind, **fields}
            with self._chat_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.previews.committed(event)
            return event

    def append_notice(self, kind, notice_id, **fields):
        """A producer retry and a startup replay deliver the same public event once."""
        with self._lock:
            if notice_id not in self._notice_ids:
                self.append(kind, notice_id=notice_id, **fields)
                self._notice_ids.add(notice_id)

    def chat(self, after: int = 0) -> list[dict]:
        with self._lock:
            if not self._chat_path.exists():
                return []
            events = []
            for line in self._chat_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    event = json.loads(line)
                    if event["seq"] > after:
                        events.append(event)
            return events

    # -- agents -----------------------------------------------------------
    def agent_dir(self, agent_id: str) -> Path:
        path = self.root / "agents" / agent_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def workspace(self, agent_id: str) -> Path:
        path = self.agent_dir(agent_id) / "workspace"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def memory(self, agent_id: str) -> str:
        path = self.agent_dir(agent_id) / "memory.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def remember(self, agent_id: str, note: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M")
        with self._lock, (self.agent_dir(agent_id) / "memory.md").open("a", encoding="utf-8") as stream:
            stream.write(f"- [{stamp}] {note.strip()}\n")

    def journal(self, agent_id: str, limit: int = 8) -> list[dict]:
        path = self.agent_dir(agent_id) / "journal.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in lines if line.strip()]

    def record_assignment(self, agent_id: str, entry: dict) -> None:
        with self._lock, (self.agent_dir(agent_id) / "journal.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": now(), **entry}, ensure_ascii=False) + "\n")

    # -- tasks ------------------------------------------------------------
    def task_dir(self, task_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id):
            raise ValueError("Invalid task ID")
        path = self.root / "tasks" / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_task(self, task: dict) -> None:
        path = self.task_dir(task["id"]) / "task.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def load_task(self, task_id: str) -> dict:
        return json.loads((self.task_dir(task_id) / "task.json").read_text(encoding="utf-8"))

    def tasks(self) -> list[dict]:
        root = self.root / "tasks"
        if not root.exists():
            return []
        rows = []
        for folder in root.iterdir():
            path = folder / "task.json"
            if path.exists():
                rows.append(json.loads(path.read_text(encoding="utf-8")))
        return sorted(rows, key=lambda row: (row.get("created_at", 0), row.get("trigger_seq", 0)))

    def trace(self, task_id: str, event: dict) -> None:
        with self._lock, (self.task_dir(task_id) / "trace.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": now(), **event}, ensure_ascii=False, default=str) + "\n")


class Store:
    """All groups under ``runs_dir/groups``."""

    def __init__(self, runs_dir: Path) -> None:
        self.root = Path(runs_dir) / "groups"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._open: dict[str, GroupStore] = {}

    def create(self, name: str, members: list[dict], lead: str, mode: str = "interactive", **options) -> GroupStore:
        group = Group(id="g-" + uuid.uuid4().hex[:12], name=name.strip(),
                      members=[Member(id=m["id"].strip(), name=(m.get("name") or m["id"]).strip(),
                                      prompt=(m.get("prompt") or "").strip(), note=m.get("note","")) for m in members],
                      lead=lead, mode=mode, **options)
        group.validate()
        root = self.root / group.id
        root.mkdir(parents=True)
        from .roles import write_json
        write_json(root / "group.json", group.to_dict())
        store = GroupStore(root, group)
        with self._lock:
            self._open[group.id] = store
        store.append("group_created", name=group.name, members=[asdict(m) for m in group.members], lead=lead, mode=mode)
        return store

    def get(self, group_id: str) -> GroupStore:
        with self._lock:
            if group_id in self._open:
                return self._open[group_id]
            if not re.fullmatch(r"g-[0-9a-f]{12}", group_id or ""):
                raise KeyError(group_id)
            path = self.root / group_id / "group.json"
            if not path.exists():
                raise KeyError(group_id)
            store = GroupStore(self.root / group_id, Group.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            self._open[group_id] = store
            return store

    def update_group(self, store: GroupStore) -> None:
        store.group.validate()
        from .roles import write_json
        write_json(store.root / "group.json", store.group.to_dict())

    def groups(self) -> list[Group]:
        rows = []
        for folder in sorted(self.root.iterdir()):
            path = folder / "group.json"
            if path.exists():
                rows.append(Group.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        return sorted(rows, key=lambda g: g.created_at, reverse=True)
