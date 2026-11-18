"""Transactional persistence for the M0-04 task/attempt/run subset.

Every operation owns its SQLite connection; BEGIN IMMEDIATE serializes writers
across processes, while revision predicates reject stale callers. Records and
outbox events commit together. Outbox delivery is at least once, not a message
bus exactly-once guarantee.

This is a trusted internal store, not an authorization boundary. It validates
M0-01 records, transitions and internal references, not a full ContractSnapshot.
Profiles, budgets, grants, external actions and resource catalogs are unresolved.
M0-02 workspace target project IDs are not equated to TaskRecord project IDs.
TaskService additionally verifies acceptance receipt closure before completion.
Checkpoint storage does not authorize or automatically resume any side effect.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .contracts.acceptance import AcceptanceReport, AcceptanceSpec, validate_report
from .contracts.base import MutableRecord, Record, freeze_json, thaw_json, time_value
from .contracts.tasks import AttemptRecord, Checkpoint, RunRecord, TaskRecord
from .contracts.transitions import TransitionError, validate_transition


class StoreError(ValueError):
    def __init__(self, message: str, code: str = "integrity_error") -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


_MODELS = {model.__name__: model for model in (
    TaskRecord, AttemptRecord, RunRecord, AcceptanceSpec, AcceptanceReport, Checkpoint,
)}
_IDS = {"TaskRecord": "task_id", "AttemptRecord": "attempt_id", "RunRecord": "run_id",
        "AcceptanceSpec": "spec_id", "AcceptanceReport": "report_id", "Checkpoint": "checkpoint_id"}
_TASK_CLOSED = {"completed", "unverified", "failed", "cancelled"}
_ATTEMPT_CLOSED = {"succeeded", "unverified", "failed", "cancelled"}
_EXECUTING = {"ready", "running", "verifying", "waiting_user", "waiting_agent", "completed", "unverified"}
_SCHEMA_COLUMNS = {
    "records": [("kind", "TEXT", 1, 1), ("id", "TEXT", 1, 2), ("version", "INTEGER", 1, 3),
                ("revision", "INTEGER", 0, 0), ("task_id", "TEXT", 1, 0), ("payload", "TEXT", 1, 0),
                ("task_kind", "TEXT", 1, 0), ("task_version", "INTEGER", 1, 0)],
    "creation_keys": [("creation_key", "TEXT", 0, 1), ("signature", "TEXT", 1, 0),
                      ("task_id", "TEXT", 1, 0), ("task_kind", "TEXT", 1, 0), ("task_version", "INTEGER", 1, 0)],
    "blobs": [("blob_id", "TEXT", 0, 1), ("kind", "TEXT", 1, 0), ("payload", "TEXT", 1, 0)],
    "outbox": [("sequence", "INTEGER", 0, 1), ("event_id", "TEXT", 1, 0), ("created_at", "TEXT", 1, 0),
               ("kind", "TEXT", 1, 0), ("task_id", "TEXT", 1, 0), ("payload", "TEXT", 1, 0),
               ("delivered_at", "TEXT", 0, 0), ("task_kind", "TEXT", 1, 0), ("task_version", "INTEGER", 1, 0)],
}


def _check(condition: bool, message: str, code: str = "integrity_error") -> None:
    if not condition:
        raise StoreError(message, code)


def _identifier(value: Any, name: str = "identifier") -> str:
    _check(type(value) is str and bool(value.strip()), f"{name} must be a nonblank string")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    try:
        value = thaw_json(freeze_json(value))
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise StoreError("payload must contain finite JSON values with string keys") from exc


def _kind(kind: Any) -> str:
    if isinstance(kind, type) and kind in _MODELS.values():
        return kind.__name__
    _check(type(kind) is str and kind in _MODELS, "unsupported record kind")
    return kind


def _record(record: Any) -> Record:
    if isinstance(record, Record):
        data = record.model_dump(mode="python", warnings=False)
    elif isinstance(record, dict):
        data = record
    else:
        raise StoreError("expected a supported contract record")
    kind = _kind(data.get("record_type"))
    _check(data.get("schema_version", 1) == 1, "unsupported record schema version", "schema_version")
    try:
        return _MODELS[kind].model_validate(data)
    except (ValueError, TypeError) as exc:
        raise StoreError(f"invalid {kind} record") from exc


def _key(record: Record) -> tuple[str, str, int]:
    kind = record.record_type
    return kind, getattr(record, _IDS[kind]), record.version if kind == "AcceptanceSpec" else 0


def _decode(row: sqlite3.Row) -> Record:
    try:
        record = _record(json.loads(row["payload"]))
        _check(_key(record) == (row["kind"], row["id"], row["version"]), "record storage identity mismatch")
        _check(getattr(record, "revision", None) == row["revision"], "record storage revision mismatch")
        _check(record.task_id == row["task_id"], "record storage task mismatch")
        return record
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise StoreError("invalid stored record payload") from exc


def _event(row: sqlite3.Row) -> dict[str, Any]:
    return {"sequence": row["sequence"], "event_id": row["event_id"], "created_at": row["created_at"],
            "kind": row["kind"], "task_id": row["task_id"], "payload": json.loads(row["payload"]),
            "delivered_at": row["delivered_at"]}


def _schema(connection: sqlite3.Connection, *, allow_empty: bool = False) -> int:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    _check(version == 1 or (version == 0 and allow_empty),
           "unsupported SQLite store schema version", "schema_version")
    if version == 0:
        objects = connection.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        _check(not objects, "unversioned database is not an empty TaskStore", "schema_version")
        return version
    for table, expected in _SCHEMA_COLUMNS.items():
        # Names come exclusively from the static schema, never caller input.
        columns = [(row["name"], row["type"], row["notnull"], row["pk"])
                   for row in connection.execute(f"PRAGMA table_info({table})")]
        _check(columns == expected, f"incompatible or missing TaskStore table: {table}", "schema_version")
    return version


class TaskStore:
    """A file-backed, reopenable store; no long-lived connection or process lock."""

    def __init__(self, path: Path | str) -> None:
        _check(str(path) != ":memory:", "TaskStore requires a file path for independent durable connections")
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass  # Existing file permissions belong to its owner.
        else:
            os.close(descriptor)
        connection = self._connect(check_schema=False)
        try:
            _schema(connection, allow_empty=True)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            version = _schema(connection, allow_empty=True)
            if version == 1:
                connection.commit()
                return
            connection.execute("""CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL,
                revision INTEGER, task_id TEXT NOT NULL, payload TEXT NOT NULL,
                task_kind TEXT NOT NULL DEFAULT 'TaskRecord' CHECK(task_kind='TaskRecord'),
                task_version INTEGER NOT NULL DEFAULT 0 CHECK(task_version=0),
                PRIMARY KEY(kind,id,version),
                FOREIGN KEY(task_kind,task_id,task_version) REFERENCES records(kind,id,version)
                    DEFERRABLE INITIALLY DEFERRED)""")
            connection.execute("CREATE INDEX IF NOT EXISTS records_task ON records(kind,task_id)")
            connection.execute("""CREATE TABLE IF NOT EXISTS creation_keys (
                creation_key TEXT PRIMARY KEY, signature TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE,
                task_kind TEXT NOT NULL DEFAULT 'TaskRecord' CHECK(task_kind='TaskRecord'),
                task_version INTEGER NOT NULL DEFAULT 0 CHECK(task_version=0),
                FOREIGN KEY(task_kind,task_id,task_version) REFERENCES records(kind,id,version)
                    DEFERRABLE INITIALLY DEFERRED)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS blobs (
                blob_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS outbox (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, kind TEXT NOT NULL, task_id TEXT NOT NULL,
                payload TEXT NOT NULL, delivered_at TEXT,
                task_kind TEXT NOT NULL DEFAULT 'TaskRecord' CHECK(task_kind='TaskRecord'),
                task_version INTEGER NOT NULL DEFAULT 0 CHECK(task_version=0),
                FOREIGN KEY(task_kind,task_id,task_version) REFERENCES records(kind,id,version)
                    DEFERRABLE INITIALLY DEFERRED)""")
            connection.execute("CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(delivered_at,sequence)")
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self, *, check_schema: bool = True) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            if check_schema:
                _schema(connection)
            return connection
        except Exception:
            connection.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[StoreTransaction]:
        connection = self._connect()
        transaction = StoreTransaction(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield transaction
            transaction._validate()
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise StoreError("SQLite integrity constraint rejected transaction") from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            transaction._active = False
            connection.close()

    @contextmanager
    def _read(self) -> Iterator[StoreTransaction]:
        connection = self._connect()
        transaction = StoreTransaction(connection, writable=False)
        try:
            yield transaction
        finally:
            transaction._active = False
            connection.close()

    def get(self, task_id: str) -> TaskRecord:
        return self.get_record("TaskRecord", task_id)

    def get_record(self, kind: str, identity: str | tuple[str, int]) -> Record:
        with self._read() as transaction:
            return transaction.get(kind, identity)

    def tasks(self, root_id: str | None = None) -> list[TaskRecord]:
        with self._read() as transaction:
            tasks = transaction.list("TaskRecord")
        return [task for task in tasks if root_id is None or task.root_id == root_id]

    def children(self, parent_id: str) -> list[TaskRecord]:
        _identifier(parent_id)
        return [task for task in self.tasks() if task.parent_id == parent_id]

    def attempts(self, task_id: str) -> list[AttemptRecord]:
        _identifier(task_id)
        with self._read() as transaction:
            return sorted((record for record in transaction.list("AttemptRecord") if record.task_id == task_id),
                          key=lambda record: record.number)

    def runs(self, attempt_id: str) -> list[RunRecord]:
        _identifier(attempt_id)
        with self._read() as transaction:
            return [record for record in transaction.list("RunRecord") if record.attempt_id == attempt_id]

    def get_blob(self, blob_id: str) -> Any:
        with self._read() as transaction:
            return transaction.get_blob(blob_id)

    def events(self, after_sequence: int = 0, limit: int = 1000, task_id: str | None = None) -> list[dict]:
        _check(type(after_sequence) is int and after_sequence >= 0, "after_sequence must be a nonnegative integer")
        _check(type(limit) is int and limit > 0, "limit must be a positive integer")
        query, args = "SELECT * FROM outbox WHERE sequence > ?", [after_sequence]
        if task_id is not None:
            query += " AND task_id = ?"
            args.append(_identifier(task_id))
        with self._read() as transaction:
            rows = transaction._connection.execute(query + " ORDER BY sequence LIMIT ?", [*args, limit])
            return [_event(row) for row in rows]

    def pending_events(self, limit: int = 1000) -> list[dict]:
        _check(type(limit) is int and limit > 0, "limit must be a positive integer")
        with self._read() as transaction:
            return [_event(row) for row in transaction._connection.execute(
                "SELECT * FROM outbox WHERE delivered_at IS NULL ORDER BY sequence LIMIT ?", (limit,))]

    def acknowledge(self, event_id: str) -> None:
        _identifier(event_id)
        with self.transaction() as transaction:
            cursor = transaction._connection.execute(
                "UPDATE outbox SET delivered_at=COALESCE(delivered_at,?) WHERE event_id=?", (_now(), event_id))
            _check(cursor.rowcount == 1, "event does not exist", "not_found")


class StoreTransaction:
    """Use only within TaskStore.transaction(); no I/O or callbacks belong here."""

    def __init__(self, connection: sqlite3.Connection, *, writable: bool = True) -> None:
        self._connection, self._active, self._writable = connection, True, writable

    def _guard(self, *, write: bool = False) -> None:
        _check(self._active, "transaction is closed")
        _check(not write or self._writable, "transaction is read only")

    def get(self, kind: str, identity: str | tuple[str, int]) -> Record:
        self._guard()
        kind = _kind(kind)
        version = None
        if isinstance(identity, tuple):
            _check(kind == "AcceptanceSpec" and len(identity) == 2, "only AcceptanceSpec accepts a versioned key")
            identity, version = identity
            _check(type(version) is int and version > 0, "spec version must be a positive integer")
        _identifier(identity)
        query, args = "SELECT * FROM records WHERE kind=? AND id=?", [kind, identity]
        if version is not None:
            query += " AND version=?"
            args.append(version)
        row = self._connection.execute(query + " ORDER BY version DESC LIMIT 1", args).fetchone()
        _check(row is not None, f"{kind} record does not exist", "not_found")
        return _decode(row)

    def list(self, kind: str) -> list[Record]:
        self._guard()
        return [_decode(row) for row in self._connection.execute(
            "SELECT * FROM records WHERE kind=? ORDER BY rowid", (_kind(kind),))]

    def insert(self, record: Record) -> Record:
        self._guard(write=True)
        record = _record(record)
        kind, identity, version = _key(record)
        payload = _json(record.model_dump(mode="json"))
        old = self._connection.execute("SELECT payload FROM records WHERE kind=? AND id=? AND version=?",
                                       (kind, identity, version)).fetchone()
        if old is not None:
            _check(old["payload"] == payload, "record ID is already bound to different data", "conflict")
            return record
        self._connection.execute(
            "INSERT INTO records(kind,id,version,revision,task_id,payload) VALUES (?,?,?,?,?,?)",
            (kind, identity, version, getattr(record, "revision", None), record.task_id, payload))
        return record

    def update(self, record: MutableRecord, expected_revision: int, command: str = "transition") -> MutableRecord:
        self._guard(write=True)
        record = _record(record)
        _check(isinstance(record, MutableRecord), "immutable records cannot be updated", "conflict")
        kind, identity, version = _key(record)
        before = self.get(kind, identity)
        try:
            validate_transition(before, record, expected_revision=expected_revision, command=command)
        except TransitionError as exc:
            raise StoreError(str(exc), "revision_conflict" if exc.code == "revision_conflict" else "integrity_error") from exc
        except ValueError as exc:
            raise StoreError("invalid record transition") from exc
        cursor = self._connection.execute(
            "UPDATE records SET payload=?, revision=? WHERE kind=? AND id=? AND version=? AND revision=?",
            (_json(record.model_dump(mode="json")), record.revision, kind, identity, version, expected_revision))
        _check(cursor.rowcount == 1, "record revision changed", "revision_conflict")
        return record

    def get_blob(self, blob_id: str) -> Any:
        self._guard()
        row = self._connection.execute("SELECT payload FROM blobs WHERE blob_id=?", (_identifier(blob_id),)).fetchone()
        _check(row is not None, "blob does not exist", "not_found")
        return json.loads(row["payload"])

    def put_blob(self, blob_id: str, kind: str, payload: Any) -> Any:
        self._guard(write=True)
        _identifier(blob_id)
        _identifier(kind, "blob kind")
        encoded = _json(payload)
        existing = self._connection.execute("SELECT kind,payload FROM blobs WHERE blob_id=?", (blob_id,)).fetchone()
        if existing is not None:
            _check(existing["kind"] == kind and existing["payload"] == encoded,
                   "blob ID is already bound to different data", "conflict")
        else:
            self._connection.execute("INSERT INTO blobs(blob_id,kind,payload) VALUES (?,?,?)", (blob_id, kind, encoded))
        return json.loads(encoded)

    def creation(self, creation_key: str) -> dict | None:
        self._guard()
        row = self._connection.execute("SELECT signature,task_id FROM creation_keys WHERE creation_key=?",
                                       (_identifier(creation_key),)).fetchone()
        return dict(row) if row is not None else None

    def bind_creation(self, creation_key: str, signature: str, task_id: str) -> None:
        self._guard(write=True)
        for value in (creation_key, signature, task_id):
            _identifier(value)
        previous = self.creation(creation_key)
        if previous is not None:
            _check(previous == {"signature": signature, "task_id": task_id},
                   "creation key is bound to a different request", "conflict")
            return
        try:
            self._connection.execute("INSERT INTO creation_keys(creation_key,signature,task_id) VALUES (?,?,?)",
                                      (creation_key, signature, task_id))
        except sqlite3.IntegrityError as exc:
            raise StoreError("task already has another creation binding", "conflict") from exc

    def emit(self, kind: str, task_id: str, payload: Any) -> dict:
        self._guard(write=True)
        _identifier(kind, "event kind")
        _identifier(task_id)
        event_id, created_at = f"event-{uuid.uuid4().hex}", _now()
        cursor = self._connection.execute(
            "INSERT INTO outbox(event_id,created_at,kind,task_id,payload) VALUES (?,?,?,?,?)",
            (event_id, created_at, kind, task_id, _json(payload)))
        row = self._connection.execute("SELECT * FROM outbox WHERE sequence=?", (cursor.lastrowid,)).fetchone()
        return _event(row)

    def _validate(self) -> None:
        """Validate the final transaction snapshot, allowing coupled writes."""
        self._guard()
        records = {kind: self.list(kind) for kind in _MODELS}
        tasks = {item.task_id: item for item in records["TaskRecord"]}
        attempts = {item.attempt_id: item for item in records["AttemptRecord"]}
        runs = {item.run_id: item for item in records["RunRecord"]}
        specs = {(item.spec_id, item.version): item for item in records["AcceptanceSpec"]}
        reports = {item.report_id: item for item in records["AcceptanceReport"]}
        checkpoints = {item.checkpoint_id: item for item in records["Checkpoint"]}
        blobs = {row[0] for row in self._connection.execute("SELECT blob_id FROM blobs")}

        def get(collection: dict, key: Any, label: str) -> Any:
            _check(key in collection, f"missing internal {label} reference")
            return collection[key]

        def owned(record: Any, task_id: str, attempt_id: str | None = None) -> None:
            _check(record.task_id == task_id, "internal reference belongs to another task")
            if attempt_id is not None:
                _check(record.attempt_id == attempt_id, "internal reference belongs to another attempt")

        def spec_for(ref: Any) -> AcceptanceSpec | None:
            return get(specs, (ref.id, ref.version), "spec version") if ref is not None else None

        children: dict[str, list[TaskRecord]] = {identity: [] for identity in tasks}
        creation_keys: set[str] = set()
        for task in tasks.values():
            _check(task.creation_key not in creation_keys, "duplicate task creation key", "conflict")
            creation_keys.add(task.creation_key)
            root = get(tasks, task.root_id, "root")
            _check(root.parent_id is None and root.project_id == task.project_id, "invalid task root/project")
            if task.parent_id is not None:
                parent = get(tasks, task.parent_id, "parent")
                _check((parent.root_id, parent.project_id) == (task.root_id, task.project_id), "parent crosses root/project")
                _check(time_value(parent.created_at) <= time_value(task.created_at), "child predates parent")
                children[parent.task_id].append(task)
            for dep_id in task.deps:
                dep = get(tasks, dep_id, "dependency")
                _check((dep.root_id, dep.project_id) == (task.root_id, task.project_id), "dependency crosses root/project")
                if task.status in _EXECUTING:
                    _check(dep.status == "completed", "execution requires completed dependencies")
            spec = spec_for(task.acceptance_ref)
            if spec is not None:
                owned(spec, task.task_id)
            if task.current_attempt_id is not None:
                owned(get(attempts, task.current_attempt_id, "current attempt"), task.task_id)
            if task.latest_report_id is not None:
                owned(get(reports, task.latest_report_id, "latest report"), task.task_id, task.current_attempt_id)
                _check(task.latest_report_id in attempts[task.current_attempt_id].report_ids,
                       "latest report is absent from attempt report history")
            if task.wait_ref is not None and task.wait_ref.kind == "children":
                for child_id in task.wait_ref.child_task_ids:
                    _check(get(tasks, child_id, "waiting child").parent_id == task.task_id, "wait references another parent's child")

        def acyclic(edges: dict[str, list[str]]) -> None:
            # Iterative DFS avoids a Python recursion limit for a large DAG.
            colors: dict[str, int] = {}
            for identity in edges:
                if colors.get(identity) == 2:
                    continue
                pending = [(identity, False)]
                while pending:
                    node, leaving = pending.pop()
                    if leaving:
                        colors[node] = 2
                    elif colors.get(node) == 1:
                        raise StoreError("task graph contains a cycle")
                    elif colors.get(node) != 2:
                        colors[node] = 1
                        pending.append((node, True))
                        pending.extend((child, False) for child in reversed(edges[node]))

        acyclic({task.task_id: list(task.deps) + [child.task_id for child in children[task.task_id]]
                 for task in tasks.values()})
        acyclic({task.task_id: [] if task.parent_id is None else [task.parent_id] for task in tasks.values()})
        for row in self._connection.execute("SELECT * FROM creation_keys"):
            _check(get(tasks, row["task_id"], "creation task").creation_key == row["creation_key"],
                   "creation binding differs from task creation key")
        for row in self._connection.execute("SELECT task_id FROM outbox"):
            get(tasks, row[0], "event task")

        by_task: dict[str, list[AttemptRecord]] = {identity: [] for identity in tasks}
        by_attempt: dict[str, list[RunRecord]] = {identity: [] for identity in attempts}
        for attempt in attempts.values():
            task = get(tasks, attempt.task_id, "attempt task")
            _check(time_value(attempt.created_at) >= time_value(task.created_at), "attempt predates task")
            by_task[attempt.task_id].append(attempt)
            if attempt.context_ref is not None:
                _check(attempt.context_ref in blobs, "missing attempt context blob")
            if attempt.checkpoint_id is not None:
                owned(get(checkpoints, attempt.checkpoint_id, "attempt checkpoint"), attempt.task_id, attempt.attempt_id)
            for report_id in attempt.report_ids:
                owned(get(reports, report_id, "attempt report"), attempt.task_id, attempt.attempt_id)
        for run in runs.values():
            attempt = get(attempts, run.attempt_id, "run attempt")
            owned(attempt, run.task_id)
            by_attempt[run.attempt_id].append(run)
            _check(time_value(run.created_at) >= time_value(attempt.created_at), "run predates attempt")
            if run.started_at is not None:
                _check(attempt.started_at is not None and time_value(run.started_at) >= time_value(attempt.started_at),
                       "run starts before attempt")
            if run.ended_at is not None and attempt.ended_at is not None:
                _check(time_value(run.ended_at) <= time_value(attempt.ended_at), "run outlives attempt")
            if run.resumed_from is not None:
                checkpoint = get(checkpoints, run.resumed_from, "resume checkpoint")
                owned(checkpoint, run.task_id, run.attempt_id)
                _check(checkpoint.run_id != run.run_id and time_value(checkpoint.created_at) <= time_value(run.created_at),
                       "invalid resume checkpoint")
        expected = {"running": "running", "verifying": "running", "waiting_user": "suspended",
                    "waiting_agent": "suspended", "completed": "succeeded", "failed": "failed",
                    "cancelled": "cancelled", "unverified": "unverified"}
        for task in tasks.values():
            family = by_task[task.task_id]
            numbers = [attempt.number for attempt in family]
            _check(len(numbers) == len(set(numbers)), "duplicate attempt number")
            _check(sum(attempt.state not in _ATTEMPT_CLOSED for attempt in family) <= 1,
                   "multiple open attempts for task")
            if family:
                current = get(attempts, task.current_attempt_id, "latest attempt")
                _check(current.number == max(numbers), "current attempt is not latest")
                _check(task.status != "created", "created task cannot have attempts")
                if task.status in expected:
                    _check(current.state == expected[task.status], "task/attempt lifecycle mismatch")
                elif task.status == "ready":
                    _check(current.state in {"pending", "suspended"}, "ready task has invalid attempt state")
            if task.status in _TASK_CLOSED:
                for child in children[task.task_id]:
                    _check(child.status in _TASK_CLOSED, "closed parent has an open descendant")
                    _check(time_value(child.updated_at) <= time_value(task.updated_at), "parent closes before child")
                if family:
                    _check(current.ended_at is not None and time_value(current.ended_at) <= time_value(task.updated_at),
                           "task closes before attempt")
                if task.latest_report_id is not None:
                    _check(time_value(reports[task.latest_report_id].created_at) <= time_value(task.updated_at),
                           "task closes before report")
            if task.status in {"completed", "unverified"}:
                report = get(reports, task.latest_report_id, "terminal report")
                _check(report.verdict == ("passed" if task.status == "completed" else "unverified")
                       and report.spec_ref == task.acceptance_ref, "terminal task requires its matching report")
                spec = spec_for(task.acceptance_ref)
                if spec is not None:
                    _check(spec.constraints_ref == task.constraints_ref, "terminal task uses stale constraints")
                if task.status == "completed":
                    evaluated = {ref.artifact_id: ref.content_hash for ref in report.evaluated_artifact_refs}
                    _check(all(evaluated.get(ref.artifact_id) == ref.content_hash for ref in task.output_artifact_refs),
                           "passed report does not cover task outputs")
        for attempt in attempts.values():
            family = by_attempt[attempt.attempt_id]
            _check(sum(run.state in {"prepared", "running"} for run in family) <= 1, "multiple open runs for attempt")
            _check((sum(run.state == "running" for run in family) == 1) == (attempt.state == "running"),
                   "attempt/run activity mismatch")
            if attempt.state in _ATTEMPT_CLOSED:
                _check(all(run.state in {"ended", "crashed"} for run in family), "closed attempt has open runs")
            ordered = sorted((run for run in family if run.started_at is not None), key=lambda run: time_value(run.started_at))
            for previous, following in zip(ordered, ordered[1:]):
                _check(previous.ended_at is not None and time_value(previous.ended_at) <= time_value(following.started_at),
                       "overlapping runs within attempt")

        for spec in specs.values():
            get(tasks, spec.task_id, "spec task")
        for report in reports.values():
            get(tasks, report.task_id, "report task")
            attempt = get(attempts, report.attempt_id, "report attempt")
            owned(attempt, report.task_id)
            try:
                validate_report(spec_for(report.spec_ref), report)
            except ValueError as exc:
                raise StoreError("report does not match its exact specification") from exc
            _check(attempt.started_at is not None and time_value(report.evaluated_at) >= time_value(attempt.started_at),
                   "report precedes attempt execution")
            if attempt.ended_at is not None:
                _check(time_value(report.created_at) <= time_value(attempt.ended_at), "report follows closed attempt")
        for checkpoint in checkpoints.values():
            task = get(tasks, checkpoint.task_id, "checkpoint task")
            owned(get(attempts, checkpoint.attempt_id, "checkpoint attempt"), task.task_id)
            run = get(runs, checkpoint.run_id, "checkpoint run")
            owned(run, task.task_id, checkpoint.attempt_id)
            _check(checkpoint.context_ref in blobs and checkpoint.history_ref in blobs, "missing checkpoint context/history blob")
            _check(checkpoint.budget_scope_id == task.budget_scope_id, "checkpoint budget identity differs from task")
            at = time_value(checkpoint.created_at)
            _check(run.started_at is not None and time_value(run.started_at) <= at, "checkpoint precedes its run")
            if run.ended_at is not None:
                _check(at <= time_value(run.ended_at), "checkpoint follows its run")
            for delegation in checkpoint.pending_delegations:
                _check(get(tasks, delegation.child_task_id, "delegated child").parent_id == task.task_id,
                       "checkpoint delegates another parent's child")
            if checkpoint.wait_ref is not None and checkpoint.wait_ref.kind == "children":
                for child_id in checkpoint.wait_ref.child_task_ids:
                    _check(get(tasks, child_id, "checkpoint waiting child").parent_id == task.task_id,
                           "checkpoint waits on another parent's child")


__all__ = ["StoreError", "StoreTransaction", "TaskStore"]
