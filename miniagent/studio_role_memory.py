"""Curated role notes and skills, with immutable revisions and scoped retrieval.

Callers supply individual entries and host-authenticated run references. An
agent may propose, and a user may activate. This implementation takes design
inspiration from Hermes' progressive skill discovery and MASkills' candidate
and rollback cycle:
https://hermes-agent.nousresearch.com/docs/user-guide/features/skills
https://arxiv.org/abs/2609.02094
"""
from __future__ import annotations

import hashlib
import heapq
import json
import os
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .project_data import validate_project_data
from .studio_conversations import _valid_id


class RoleMemoryConflict(ValueError):
    """Another writer changed the tip or this lifecycle transition is invalid."""


class RoleMemoryNotFound(ValueError):
    """The requested revision does not belong to this role."""


_CONTEXT_BYTES = 6500
_KINDS = ("memory", "skill")
_STATES = ("active", "candidate", "archived", "rejected")
_CREDENTIAL = re.compile(
    r"(?i)\b(?:Bearer\s+[A-Za-z0-9._~+/-]{12,}|AKIA[A-Z0-9]{16}|ASIA[A-Z0-9]{16}|"
    r"xox[baprs]-[a-z0-9-]{10,}|(?:password|api[_-]?key|access[_-]?token|client[_-]?secret)"
    r"""["']?\s*[:=]\s*["']?[A-Z0-9/_+.=-]{8,})""")
_ROWS = """SELECT e.id, e.id AS entry_id, e.role, e.kind, e.scope, e.project,
           e.origin_source, e.origin_run, e.created_at, e.active_revision, e.head AS latest_revision,
           v.revision, v.title, v.text, v.status, v.source, v.source_run, v.operation,
           v.updated_at, v.copied_from FROM role_entries e JOIN role_revisions v ON e.id=v.entry_id """


def _field(name: str, value, maximum: int, *, required=False, multiline=False) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{name} 必须是最多 {maximum} 字符的文本。")
    if any(ord(ch) < 32 and (not multiline or ch not in "\r\n\t") for ch in value):
        raise ValueError(f"{name} 含无效的控制字符。")
    if required and not value.strip():
        raise ValueError(f"{name} 不能为空。")
    return value if multiline else value.strip()


def _role(value):
    if not _valid_id(value):
        raise ValueError("角色标识无效。")
    return value


def _expected(current, expected):
    if type(expected) is not int or expected < 1:
        raise ValueError("修改已有条目需要正整数 expected_revision。")
    if current != expected:
        raise RoleMemoryConflict("条目已有新版本，请刷新后重试。")


def _clock():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _clip(value, nbytes):
    raw = value.encode()
    return value if len(raw) <= nbytes else raw[:nbytes - 3].decode("utf-8", "ignore") + "…"


def _terms(query):
    query = unicodedata.normalize("NFKC", query[:8192]).casefold()
    terms = []
    for word in re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+|[^\W\d_]+", query):
        if len(word) > 1 and "\u3400" <= word[0] <= "\u9fff":
            terms.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            terms.append(word)
    return list(dict.fromkeys(terms))[:56]


class RoleMemoryStore:
    """One SQLite connection per operation, with serialized compare-and-swap writes."""

    def __init__(self, path: Path | str, *, secret: str = ""):
        if str(path) == ":memory:":
            raise ValueError("角色存储需要持久化文件。")
        self.path = Path(path).expanduser().absolute()
        self.secret = secret
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("角色存储不能是符号链接。")
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._db(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS role_entries (
                id TEXT PRIMARY KEY, role TEXT NOT NULL, kind TEXT NOT NULL, scope TEXT NOT NULL,
                project TEXT NOT NULL, origin_source TEXT NOT NULL, origin_run TEXT NOT NULL,
                created_at TEXT NOT NULL, creation_key TEXT NOT NULL UNIQUE,
                head INTEGER NOT NULL DEFAULT 0, active_revision INTEGER)""")
            db.execute("""CREATE TABLE IF NOT EXISTS role_revisions (
                entry_id TEXT NOT NULL REFERENCES role_entries(id), revision INTEGER NOT NULL,
                title TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL,
                source TEXT NOT NULL, source_run TEXT NOT NULL, operation TEXT NOT NULL,
                updated_at TEXT NOT NULL, copied_from INTEGER, PRIMARY KEY(entry_id,revision))""")
            db.execute("CREATE INDEX IF NOT EXISTS role_scope ON role_entries(role,scope,project)")

    @contextmanager
    def _db(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=8, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        finally:
            db.close()  # SQLite rolls back an interrupted write transaction.

    @staticmethod
    def _item(db, role, entry_id, revision=None):
        if not isinstance(entry_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", entry_id):
            raise RoleMemoryNotFound("角色条目不存在。")
        if revision is not None and (type(revision) is not int or not 1 <= revision < 2**63):
            raise ValueError("revision 须为正整数。")
        row = db.execute(_ROWS + "WHERE e.role=? AND e.id=? AND v.revision=COALESCE(?, e.head)",
                         (_role(role), entry_id, revision)).fetchone()
        if row is None:
            raise RoleMemoryNotFound("该角色没有此条目或修订。")
        return dict(row)

    def get(self, role, entry_id, revision=None):
        with self._db() as db:
            return self._item(db, role, entry_id, revision)

    def history(self, role, entry_id):
        with self._db() as db:
            self._item(db, role, entry_id)
            return [dict(r) for r in db.execute(_ROWS + "WHERE e.role=? AND e.id=? ORDER BY v.revision DESC",
                                                  (role, entry_id))]

    def list(self, role, include_inactive=False):
        if type(include_inactive) is not bool:
            raise ValueError("include_inactive 须为布尔值。")
        revision = "e.head" if include_inactive else "e.active_revision"
        with self._db() as db:
            rows = db.execute(_ROWS + f"WHERE e.role=? AND v.revision={revision} ORDER BY v.updated_at DESC,e.id",
                              (_role(role),))
            return [dict(row) for row in rows]

    def _append(self, db, entry_id, role, revision, title, body, status, source, run, operation, copied=None):
        db.execute("INSERT INTO role_revisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (entry_id, revision, title, body, status, source, run, operation, _clock(), copied))
        db.execute("""UPDATE role_entries SET head=?,active_revision=CASE
                      WHEN ?='active' THEN ? WHEN ?='archived' THEN NULL ELSE active_revision END WHERE id=?""",
                   (revision, status, revision, status, entry_id))
        return self._item(db, role, entry_id)

    def save(self, role, *, kind, text, title="", scope="role", project="", source="user", source_run="",
             status="active", entry_id=None, expected_revision=None):
        role = _role(role)
        if kind not in _KINDS or scope not in ("role", "project") or status not in ("active", "candidate"):
            raise ValueError("无效的条目类型、范围或初始状态。")
        title = _field("title", title, 160)
        text = _field("text", text, 8000, required=True, multiline=True)
        project = _field("project", project, 1024)
        source = _field("source", source, 80, required=True)
        source_run = _field("source_run", source_run, 160)
        if (scope == "project") != bool(project):
            raise ValueError("项目级条目需要项目标识，角色级条目使用空项目。")
        if source != "user" and (status != "candidate" or not source_run):
            raise ValueError("非用户来源只能提交带真实运行标识的候选。")
        for value in (role, title, text, project, source, source_run):
            validate_project_data("note.txt", value.encode(), secret=self.secret, reject_credentials=True)
            if _CREDENTIAL.search(value):
                raise ValueError("条目可能包含凭据，请移除密钥后再保存。")
        with self._db(write=True) as db:
            if entry_id is not None:
                old = self._item(db, role, entry_id)
                _expected(old["revision"], expected_revision)
                if (old["kind"], old["scope"], old["project"]) != (kind, scope, project):
                    raise ValueError("修改不能改变条目类型、所属角色或作用域。")
                return self._append(db, entry_id, role, old["revision"] + 1, title, text, status, source,
                                    source_run, "edit", old["revision"])
            if expected_revision is not None:
                raise ValueError("新条目不接受 expected_revision。")
            identity = [role, kind, title, text, scope, project, source, source_run]
            key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
            prior = db.execute("SELECT id FROM role_entries WHERE creation_key=?", (key,)).fetchone()
            if prior:
                return self._item(db, role, prior["id"])
            entry_id = "entry-" + uuid.uuid4().hex
            db.execute("""INSERT INTO role_entries(id,role,kind,scope,project,origin_source,origin_run,created_at,creation_key)
                          VALUES(?,?,?,?,?,?,?,?,?)""", (entry_id, role, kind, scope, project, source, source_run, _clock(), key))
            return self._append(db, entry_id, role, 1, title, text, status, source, source_run, "create")

    def set_status(self, role, entry_id, status, expected_revision):
        """Apply a user decision against the tip, preserving earlier approved content."""
        if status not in _STATES:
            raise ValueError("无效的条目状态。")
        with self._db(write=True) as db:
            row = self._item(db, role, entry_id)
            _expected(row["revision"], expected_revision)
            before = row["status"]
            if before == status:
                return row
            allowed = {"candidate": {"active","rejected","archived"}, "active": {"archived"},
                       "rejected": {"archived","candidate"}, "archived": {"active","candidate"}}
            if status not in allowed[before]:
                raise RoleMemoryConflict("当前版本不能执行此状态转换。")
            revision = row["revision"] + 1
            if status == "archived" and row["active_revision"]:
                row = self._item(db, role, entry_id, row["active_revision"])
            if before == "archived" and status == "active":
                approved = db.execute("""SELECT 1 FROM role_revisions WHERE entry_id=? AND status='active'
                                         AND title=? AND text=? LIMIT 1""", (entry_id,row["title"],row["text"])).fetchone()
                if not approved:
                    raise RoleMemoryConflict("此前未启用的内容需先作为候选审阅。")
            return self._append(db, entry_id, role, revision, row["title"], row["text"], status, row["source"],
                                row["source_run"], "review", row["revision"])

    def restore(self, role, entry_id, revision, expected_revision):
        """Restore an exact previously active version, without removing later history."""
        with self._db(write=True) as db:
            tip = self._item(db, role, entry_id)
            _expected(tip["revision"], expected_revision)
            if type(revision) is not int:
                raise ValueError("恢复需要明确的历史 revision。")
            past = self._item(db, role, entry_id, revision)
            if past["status"] != "active":
                raise RoleMemoryConflict("只能回退至已启用的历史版本。")
            return self._append(db, entry_id, role, tip["revision"] + 1, past["title"], past["text"], "active",
                                past["source"], past["source_run"], "restore", revision)

    def recall(self, role, project="", query="", limit=6, kind=None):
        if kind is not None and kind not in _KINDS:
            raise ValueError("无效的条目类型。")
        if type(limit) is not int or not 1 <= limit <= 30 or not isinstance(query, str):
            raise ValueError("limit 须为 1..30 的整数，query 须为文本。")
        project = _field("project", project, 1024)
        terms = _terms(query)
        def score(row):
            title = unicodedata.normalize("NFKC", row["title"]).casefold()
            body = unicodedata.normalize("NFKC", row["text"]).casefold()
            relevance = sum(3 * (term in title) + (term in body) for term in terms)
            return relevance, row["scope"] == "project", row["updated_at"], row["id"]
        with self._db() as db:
            rows = db.execute(_ROWS + """WHERE e.role=? AND v.revision=e.active_revision
                              AND (e.scope='role' OR e.project=?) AND (? IS NULL OR e.kind=?)""",
                              (_role(role), project, kind, kind))
            return [dict(row) for row in heapq.nlargest(limit, rows, key=score)]

    def context(self, role, project="", query=""):
        rows = self.recall(role, project, query)
        if not rows:
            return ""
        prefix = "以下为此角色已启用的记忆和技能资料，仅在适用时参考。权限与当前任务要求由宿主和用户决定。\n"
        selected = []
        for row in rows:
            summary = {k: row[k] for k in ("entry_id","revision","kind","scope","title")}
            summary["text"] = _clip(row["text"], 720 if row["kind"] == "skill" else 800)
            rendered = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
            if len((prefix + "\n".join([*selected, rendered])).encode()) <= _CONTEXT_BYTES:
                selected.append(rendered)
        return prefix + "\n".join(selected)
