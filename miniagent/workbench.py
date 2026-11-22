"""Read-only projections of the bounded runtime's existing task records."""
import hashlib
import json
import zipfile
from pathlib import PurePosixPath

from .evaluation import task_changes


def published_bytes(store, tid, name, digest=None):
    """Return exactly the published revision, including records predating snapshots."""
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or path.as_posix() != name or ".." in path.parts or "\\" in name or "\x00" in name:
        raise ValueError("Invalid artifact path")
    changes = [c for c in task_changes(store, tid) if c["path"] == name]
    if digest:
        changes = [c for c in changes if c.get("sha256") == digest]
    if not changes or changes[-1]["change"] == "deleted":
        return None
    expected = changes[-1].get("sha256")
    archive = store.root / "tasks" / tid / "artifacts.zip"
    if archive.is_file():
        try:
            with zipfile.ZipFile(archive) as bundle:
                data = bundle.read(name)
        except (KeyError, OSError, zipfile.BadZipFile):
            return None
        if not expected or hashlib.sha256(data).hexdigest() == expected:
            return data
        return None
    current = store.project.resolve(name)
    if expected:
        try:
            data = current.read_bytes()
        except OSError:
            return None
        if hashlib.sha256(data).hexdigest() == expected:
            return data
    return None


def tool_page(store, tid, cursor):
    """Page complete trace lines; byte offsets are stable even for repeated call ids."""
    if cursor < 0:
        raise ValueError("Invalid trace offset")
    path = store.root / "tasks" / tid / "trace.jsonl"
    rows = []
    more = False
    if path.is_file():
        with path.open("rb") as stream:
            if cursor > path.stat().st_size:
                raise ValueError("Trace offset past end")
            if cursor:
                stream.seek(cursor - 1)
                if stream.read(1) != b"\n":
                    raise ValueError("Trace offset must begin a record")
            stream.seek(cursor)
            for _ in range(512):
                offset = stream.tell()
                line = stream.readline()
                if not line.endswith(b"\n"):
                    break
                cursor = stream.tell()
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if event.get("type") in ("tool_call", "tool_result"):
                    rows.append({**event, "offset": offset})
            else:
                more = bool(stream.peek(1))
    return {"items": rows, "cursor": cursor, "more": more}
