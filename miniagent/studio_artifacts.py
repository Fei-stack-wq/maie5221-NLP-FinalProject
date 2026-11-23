"""Delete published deliverables by session and logical path, including saved revisions."""
import json
import os
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .folders import checked
from .roles import write_json
from .workspace import _digest


def relative_name(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("Expected a published relative file path")
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        raise ValueError("Expected a published relative file path")
    return name


@contextmanager
def replacement(path):
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    os.close(fd)
    tmp = Path(tmp)
    try:
        yield tmp
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def unlink_matching(path, expected):
    # An editor may save by replacing the inode while a large file is being hashed.
    first = path.lstat()
    if path.is_symlink() or _digest(path) not in expected:
        return False
    last = path.lstat()
    if (first.st_ino, first.st_size, first.st_mtime_ns) != (last.st_ino, last.st_size, last.st_mtime_ns):
        return False
    path.unlink()
    return True


def publications(room):
    records = {}
    scope = os.path.relpath(room.project.root, room.project.log_path.parent)
    for entry in room.project.history(0):
        for change in entry["changes"]:
            name = change["path"]
            item = records.setdefault(name, {"path":name, "versions":0, "task_ids":set(), "digests":set()})
            item["task_ids"].add(entry["task_id"])
            if change.get("sha256"):
                item["digests"].add(change["sha256"])
                item["versions"] += 1
            if entry.get("project_scope",scope) == scope:
                item["bound_digest"] = change.get("sha256")
            if change.get("sha256"):
                item.setdefault("scopes",{}).setdefault(entry.get("project_scope","legacy"),set()).add(change["sha256"])
    return records


def strip_snapshot(path, names):
    with zipfile.ZipFile(path) as source:
        if names.isdisjoint(source.namelist()):
            return False
        with replacement(path) as tmp, zipfile.ZipFile(tmp, "w") as target:
            for member in source.infolist():
                if member.filename not in names:
                    with source.open(member) as reader, target.open(member, "w") as writer:
                        shutil.copyfileobj(reader, writer)
    return True


def plan_deletion(room, paths=None, all_files=False, *, shared=()):
    pending = room.root / ".pending-artifact-delete.json"
    records = publications(room)
    if all_files is True and paths is None:
        names = sorted(records)
    elif all_files is False and isinstance(paths, list) and paths:
        names = list(dict.fromkeys(relative_name(p) for p in paths))
        missing = [p for p in names if p not in records]
        if missing:
            raise ValueError("未找到发布记录：" + "、".join(missing))
    else:
        raise ValueError("请提供具体产物路径，或明确选择该会话的全部产物。")
    baseline = json.loads(room.folder_state.read_bytes()) if room.folder_state.exists() else {}
    files = []
    for name in names:
        relative_name(name)
        last = records[name].get("bound_digest")
        synced = baseline.get("files", {}).get(name) if baseline.get("folder") == room.group.folder else None
        files.append({"name":name, "digests":sorted(records[name]["digests"]),
                      "scopes":{k:sorted(v) for k,v in records[name].get("scopes",{}).items()},
                      "shared":name in shared, "external_digest":last if last and last == synced else None})
    write_json(pending, {"files":files, "folder":room.group.folder})


def finish_deletion(room):
    pending = room.root / ".pending-artifact-delete.json"
    if room.root.resolve() != room.root.absolute() or pending.is_symlink():
        raise ValueError("Unexpected conversation location")
    plan = json.loads(pending.read_bytes())
    old_folder = plan["folder"]
    roots = [room.root / "project", *(room.root / "workspaces").glob("*/project"),
             *(room.root / "agents").glob("*/workspace")]
    kept, removed = [], []
    names = {relative_name(f["name"]) for f in plan["files"]}
    # Check each path at the moment of deletion. A user edit following publication wins.
    for f in plan["files"]:
        name = f["name"]
        for root in roots:
            if root.is_dir() and root.resolve() == root.absolute():
                try:
                    path = checked(root.resolve(), name)
                except ValueError:
                    kept.append({"path":name, "location":"studio","reason":"link_changed"})
                    continue
                if path.is_file():
                    scope = os.path.relpath(root,room.root)
                    matching = f["digests"] if scope.startswith("agents/") else (
                        f["scopes"].get(scope,[]) + f["scopes"].get("legacy",[]))
                    if not unlink_matching(path,matching):
                        kept.append({"path":name, "location":"studio", "reason":"content_changed"})
        if not old_folder or old_folder != room.group.folder:
            continue
        folder = Path(old_folder)
        if not folder.is_dir() or folder.resolve() != folder:
            kept.append({"path":name, "location":"workspace", "reason":"unavailable"})
            continue
        try:
            external = checked(folder, name)
        except ValueError:
            kept.append({"path":name, "location":"workspace", "reason":"link_changed"})
            continue
        if external.exists() and not external.is_file():
            kept.append({"path":name,"location":"workspace","reason":"type_changed"})
        if external.is_file():
            if f.get("shared"):
                kept.append({"path":name,"location":"workspace","reason":"shared_with_another_session"})
            elif f["external_digest"] and unlink_matching(external,[f["external_digest"]]):
                removed.append(name)
            else:
                kept.append({"path":name, "location":"workspace", "reason":"modified_or_unverified"})
    for archive in (room.root / "tasks").glob("t-*/artifacts.zip"):
        if archive.resolve() != archive.absolute():
            raise ValueError("Unexpected snapshot location")
        strip_snapshot(archive, names)
    log = room.project.log_path
    if log.exists():
        with replacement(log) as tmp, log.open(encoding="utf-8") as src, tmp.open("w",encoding="utf-8") as dst:
            for line in src:
                if line.strip():
                    row = json.loads(line)
                    row["changes"] = [c for c in row["changes"] if c["path"] not in names]
                    if row["changes"]:
                        dst.write(json.dumps(row,ensure_ascii=False)+"\n")
    return {"operation":"delete_artifacts", "session_id":room.group.id, "deleted_paths":sorted(names),
            "historical_snapshots_cleaned":True, "workspace_deleted":removed, "preserved":kept}
