"""Reusable role definitions and append-only, explicitly approved knowledge."""
from contextlib import closing
import json
import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from .store import Member, now


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class Roles:
    def __init__(self, root):
        self.root = Path(root) / "roles"
        self.lock = threading.RLock()

    def path(self, role):
        if not isinstance(role, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,47}", role):
            raise ValueError("Invalid role ID")
        return self.root / role

    def list(self):
        return [json.loads(p.read_bytes()) for p in sorted(self.root.glob("*/role.json"))]

    def get(self, role):
        p = self.path(role) / "role.json"
        if not p.is_file():
            raise ValueError("Unknown role: " + role)
        return json.loads(p.read_bytes())

    def save(self, data, *, update=False):
        role = data["id"]
        with self.lock:
            p = self.path(role) / "role.json"
            existing = self.get(role) if update else {}
            if not update and p.exists():
                raise ValueError("Role already exists")
            values = {**existing, **data}
            member = Member(role, values["name"], values["prompt"])
            member.validate()
            record = {"id":member.id,"name":member.name,"prompt":member.prompt, "created_at": existing.get("created_at", now()), "updated_at": now()}
            write_json(p, record)
            return record

    def history(self, role):
        path = self.path(role) / "knowledge.jsonl"
        with self.lock:
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def entries(self, role, *, group=None):
        latest = {row["id"]: row for row in self.history(role)}
        if group is not None:
            return [v for v in latest.values() if v["status"] == "approved" and v["scope"] in ("general", "group:" + group)]
        return list(latest.values())

    def _append(self, role, entry):
        folder = self.path(role)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "knowledge.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def propose(self, role, text, scope, kind="memory", identity=None):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 12000:
            raise ValueError("Knowledge text must contain 1–12000 characters")
        if scope != "general" and not re.fullmatch(r"group:[A-Za-z0-9_-]{1,90}", scope):
            raise ValueError("Invalid knowledge scope")
        if kind not in ("memory", "skill"):
            raise ValueError("Unknown knowledge type")
        with self.lock:
            return self._append(role, dict(id=identity or uuid.uuid4().hex, scope=scope, kind=kind, text=text,
                                           status="proposed", revision=1, updated_at=now()))

    def revise(self, role, identity, status, *, text=None, revision=None):
        with self.lock:
            versions = [row for row in self.history(role) if row["id"] == identity]
            if not versions or status not in ("approved", "rejected", "archived", "restore"):
                raise ValueError("Unknown entry or status")
            row = versions[-1]
            if status == "restore":
                row = next((v for v in reversed(versions) if v["revision"] == revision), None)
                if row is None:
                    raise ValueError("Unknown revision")
                status = row["status"]
            if text is not None and (not isinstance(text, str) or not 1 <= len(text.strip()) <= 12000):
                raise ValueError("Invalid knowledge text")
            return self._append(role, {**row, "text": row["text"] if text is None else text, "status": status,
                                       "revision": versions[-1]["revision"]+1, "updated_at": now()})

    def migrate(self, source):
        profiles = Path(source) / "studio-profiles.json"
        counts = {"roles": 0, "knowledge": 0}
        if profiles.exists():
            for entry in json.loads(profiles.read_bytes()):
                if not (self.path(entry["id"]) / "role.json").exists():
                    self.save(entry)
                    counts["roles"] += 1
        db = Path(source) / "studio-role-memory.sqlite3"
        if db.is_file():
            with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                for role, identity, scope, project, kind, body, origin, source_type in conn.execute(
                    """SELECT e.role,e.id,e.scope,e.project,e.kind,v.text,e.origin_run,e.origin_source
                       FROM role_entries e JOIN role_revisions v ON v.entry_id=e.id AND v.revision=e.active_revision
                       WHERE v.status='active'"""):
                    # Never promote private/project knowledge to global on migration.
                    portable = scope == "role" and not (source_type == "user" and origin)
                    scoped = "general" if portable else "group:" + str(project or origin or identity)
                    if not (self.path(role)/"role.json").exists():
                        self.save({"id":role,"name":role,"prompt":""})
                        counts["roles"] += 1
                    if not any(r["id"] == identity for r in self.entries(role)):
                        self.propose(role, body, scoped, kind, identity)
                        self.revise(role, identity, "approved")
                        counts["knowledge"] += 1
        # Old bounded-core notes did not have a user approval flag. Import as proposals.
        import hashlib
        for note in Path(source).glob("groups/*/agents/*/memory.md"):
            role = note.parent.name
            gid = note.parents[2].name
            scope = "group:" + gid
            if not (self.path(role) / "role.json").exists():
                group_file = note.parents[2] / "group.json"
                members = json.loads(group_file.read_bytes()).get("members", []) if group_file.exists() else []
                definition = next((m for m in members if m["id"] == role), {"id":role, "name":role, "prompt":""})
                self.save(definition)
                counts["roles"] += 1
            for line in note.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                identity = hashlib.sha256((gid+"\0"+role+"\0"+line).encode()).hexdigest()
                if not any(row["id"] == identity for row in self.entries(role)):
                    self.propose(role,line,scope,identity=identity)
                    counts["knowledge"] += 1
        return counts
