"""Deliver only the public files and answer of the specific routed turn."""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from uuid import UUID

from . import studio_workspace as workspace
from .project_data import is_database, is_png, validate_project_data


def _json_record(path, name):
    from .studio_service import StudioError, _read_file
    raw, clipped = _read_file(path, name, 4 * 1024**2, missing=True)
    if clipped:
        raise StudioError("派工交付清单超过工作区上限。", "invalid_delivery", 409)
    return json.loads(raw) if raw else {}


def _referenced_file(service, source, reference):
    from .studio_service import StudioError
    key, separator, name = reference.removeprefix("dispatch:").partition("/")
    try:
        valid = str(UUID(key)) == key
    except ValueError:
        valid = False
    if not separator or not valid:
        raise StudioError("请从原对话选择已交付文件的完整引用。", "invalid_delivery")
    row = service.studio_dispatch_reference(service._meta(source), key)
    if row and row["status"] in {"completed", "unverified"}:
        child = service._directory(row["child_run_id"])
        for item in _json_record(child, "dispatch-result.json").get("artifacts", []):
            if item["project_path"] == name:
                data, clipped = service._artifact_data(child.name, item["path"], 8 * 1024**2)
                if not clipped and hashlib.sha256(data).hexdigest() == item["sha256"]:
                    return name, data
    raise StudioError("本项目中找不到这份已交付的文件。", "invalid_delivery", 409)


def delivery_catalog(service, source, query="", offset=0, *, exact=False):
    from .studio_service import StudioError
    records = service._read_dispatch_rows(
        "SELECT * FROM dispatches WHERE scope=? AND state='started' ORDER BY rowid DESC",
        (service._dispatch_scope(source),))
    items = []
    for record in records:
        receipt = service._dispatch_receipt(record)
        if receipt["status"] not in {"completed", "unverified"}:
            continue
        path = service._directory(receipt["child_run_id"])
        for artifact in _json_record(path, "dispatch-result.json").get("artifacts", []):
            matches = (query == artifact["project_path"] if exact else query.casefold() in artifact["project_path"].casefold())
            if matches:
                items.append({"project_path": artifact["project_path"], "ref": artifact["ref"],
                              "author": receipt["target_name"], "size": artifact["size"]})
    if type(offset) is not int or not 0 <= offset <= len(items):
        raise StudioError("已交付文件的分页位置无效。")
    return {"files": items[offset:offset + 16], "total": len(items),
            "next_offset": offset + 16 if offset + 16 < len(items) else None}


def bind_inputs(service, directory, source, requested):
    """When the model names an input, pin its unique returned version at admission."""
    from .studio_service import StudioError, _EXTENSIONS
    project = service._workspace(directory, source["thread"])
    bound = []
    for reference in requested:
        if not reference.startswith("dispatch:"):
            if not workspace._project_path(reference, _EXTENSIONS):
                raise StudioError("请选择项目中支持交接的相对文件路径。", "invalid_dispatch_input")
            if _workspace_file(project, reference) is None:
                found = delivery_catalog(service, source, query=reference, exact=True)
                if found["total"] == 0:
                    raise StudioError("未找到指定输入，请检查工作区或通过 list_studio_deliveries 查询已交付原件。", "missing_dispatch_input")
                if found["total"] > 1:
                    raise StudioError("同名交付有多个版本，请通过 list_studio_deliveries 选择具体 ref。", "ambiguous_dispatch_input")
                reference = found["files"][0]["ref"]
        bound.append(reference)
    return bound


def read_delivery(service, source, ref, offset=0):
    from .studio_service import StudioError
    name, data = _referenced_file(service, source, ref)
    if is_database(name) or is_png(name):
        raise StudioError("二进制交付可以通过 ref 原样转交，或在聊天中打开预览。")
    try:
        content = data.decode("utf-8")
    except UnicodeError:
        raise StudioError("二进制交付可以通过 ref 原样转交，或在聊天中打开预览。") from None
    if type(offset) is not int or not 0 <= offset <= len(content):
        raise StudioError("文件文本分页位置无效。")
    end = min(offset + 6000, len(content))
    return {"project_path": name, "content": content[offset:end], "next_offset": end if end < len(content) else None}


def imported_review_candidate(service, crew, reference, manifest, overwritten):
    """A directly assigned role can be identified as the author of its frozen file."""
    child = service._meta(crew.path)
    route = child.get("request", {}).get("studio_dispatch") or {}
    if not route or not route.get("input_files"):
        return None
    source = service._directory(route["source_run_id"])
    source_meta = service._meta(source)
    for ref in route["input_files"]:
        if not ref.startswith("dispatch:"):
            continue
        dispatch_id, _, name = ref[len("dispatch:"):].partition("/")
        if name != reference or name in overwritten or name not in manifest:
            continue
        row = service.studio_dispatch_reference(source_meta, dispatch_id)
        if not row or row["target_kind"] != "role" or row["status"] not in {"completed", "unverified"}:
            continue
        _, data = _referenced_file(service, source, ref)
        digest = workspace._digest(data)
        if digest == manifest[name]:
            return {"task_id": row["child_run_id"], "run_id": row["child_run_id"], "role": row["target_id"],
                    "objective": row["objective"], "files": {name: digest}, "unresolved": []}
    return None


def _workspace_file(directory, name):
    # Consumers pass a checked project-relative path. Pin every parent directory
    # and read a complete regular file, including the SQLite sidecar checks.
    path = directory / name
    try:
        with workspace._directory(path.parent) as parent:
            workspace._check_database_idle(parent, path.name)
            data = workspace._contents(parent, path.name)
            workspace._check_database_idle(parent, path.name)
    except FileNotFoundError:
        return None
    return data


def _inherited_input_ref(service, inherited_from, name):
    """Trace the actual file-copy chain to the most recent import of this path."""
    visited = set()
    while inherited_from and inherited_from not in visited:
        visited.add(inherited_from)
        meta = service._meta(service._directory(inherited_from))
        thread = meta.get("thread") or {}
        if meta.get("workspace_path") or thread.get("recover_failed"):
            break
        route = meta.get("request", {}).get("studio_dispatch") or {}
        for value in route.get("input_files", []):
            if value == name:
                return None  # This version came from an ordinary workspace input.
            if value.startswith("dispatch:") and value.partition("/")[2] == name:
                return value
        inherited_from = thread.get("base_run_id")
    return None


def _is_input_revision(service, source, inherited_from, reference, name, existing):
    if not inherited_from or not reference.startswith("dispatch:"):
        return False
    previous = _inherited_input_ref(service, inherited_from, name)
    if not previous:
        return False
    source_meta = service._meta(source)
    old, new = [service.studio_dispatch_reference(source_meta, ref.removeprefix("dispatch:").partition("/")[0])
                for ref in (previous, reference)]
    if not old or not new or old["status"] not in {"completed", "unverified"}:
        return False
    if any(old[key] != new[key] for key in ("target_kind", "target_id", "conversation_id")):
        return False
    old_turn = service._meta(service._directory(old["child_run_id"]))["thread"]
    new_turn = service._meta(service._directory(new["child_run_id"]))["thread"]
    return (old_turn["ordinal"] < new_turn["ordinal"]
            and _referenced_file(service, source, previous)[1] == existing)


def prepare_inputs(service, source_id, staged, target, input_files, *, inherited_from=None):
    from .studio_service import StudioError, _EXTENSIONS, _write_json
    source = service._directory(source_id)
    origin = service._workspace(source, service._meta(source)["thread"])
    target.mkdir(parents=True, exist_ok=True)
    total = 0
    for reference in input_files:
        name = reference.partition("/")[2] if reference.startswith("dispatch:") else reference
        if not workspace._project_path(name, _EXTENSIONS):
            raise StudioError("派工输入需要项目内支持的相对文件路径。", "invalid_dispatch_input")
        if reference.startswith("dispatch:"):
            name, data = _referenced_file(service, source, reference)
        else:
            data = _workspace_file(origin, name)
        if data is None:
            raise StudioError("指定的派工输入文件不存在。", "missing_dispatch_input", 409)
        total += len(data)
        if total > 20 * 1024**2:
            raise StudioError("派工输入超过项目快照的总量上限。", "invalid_dispatch_input")
        validate_project_data(name, data, secret=service.settings.api_key, reject_credentials=True)
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        previous = _workspace_file(target, name)
        if previous is None:
            # Only the private staging directory is mutated before admission.
            with dest.open("xb") as stream:
                stream.write(data)
        elif previous != data:
            if not _is_input_revision(service, source, inherited_from, reference, name, previous):
                raise StudioError("派工输入与目标工作区的同名文件内容不同。", "dispatch_input_conflict", 409)
            # A later version from the same producing conversation can update
            # its own unchanged import in this private checkout.
            with workspace._directory(dest.parent) as parent:
                workspace._replace(parent, dest.name, data, 0o600, expected=previous)
    with workspace._directory(target) as root:
        initial = workspace._scan(root, _EXTENSIONS, service.settings.api_key)
    _write_json(staged, "dispatch-inputs.json", {name: workspace._digest(data) for name, data in initial.items()})


def capture_delivery(service, child, meta):
    from .studio_service import _EXTENSIONS, _write_json
    key = str(UUID(meta["studio_dispatch"]["dispatch_id"]))
    baseline = _json_record(child, "dispatch-inputs.json")
    declared = set(meta["request"]["acceptance_files"])
    with workspace._directory(service._workspace(child, meta["thread"])) as root:
        outputs = workspace._scan(root, _EXTENSIONS, service.settings.api_key)
    artifacts, manifest = [], {}
    snapshots = child / "handoffs"
    snapshots.mkdir(exist_ok=True)
    snapshot = UUID(key).hex
    # Reuse the signed handoff reader. A chat link keeps the exact bytes this
    # child returned, even if a later DM turn edits a same-named file.
    with tempfile.TemporaryDirectory(dir=child, prefix=".route-delivery-") as temp:
        frozen = Path(temp) / "snapshot"
        (frozen / "project").mkdir(parents=True)
        for name, data in sorted(outputs.items()):
            digest = workspace._digest(data)
            if name not in declared and baseline.get(name) == digest:
                continue
            target = frozen / "project" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            manifest[name] = digest
            artifacts.append({"name": Path(name).name, "project_path": name, "size": len(data), "sha256": digest,
                              "path": f"handoffs/{snapshot}/{name}", "ref": f"dispatch:{key}/{name}"})
        _write_json(frozen, "manifest.json", manifest)
        frozen.rename(snapshots / snapshot)
    _write_json(child, "dispatch-result.json", {"artifacts": artifacts})


def project_dispatches(service, source_id):
    from .studio_service import _read_file
    rows = service.studio_dispatches(source_id)
    result = []
    for row in rows:
        entry = {**row, "answer": "", "artifacts": [], "pending_questions": []}
        result.append(entry)
        child_id = row.get("child_run_id")
        if not child_id or row["status"] == "queued":
            continue
        child = service._directory(child_id)
        if row["status"] in {"completed", "unverified"}:
            answer, clipped = _read_file(child, "conversation-answer.txt", 8192, missing=True)
            entry.update(answer=answer.decode("utf-8", errors="ignore"), answer_truncated=clipped,
                         artifacts=_json_record(child, "dispatch-result.json").get("artifacts", []))
        elif row["status"] == "waiting_user":
            entry["pending_questions"] = [
                {k: q[k] for k in ("request_id", "role", "text")} for (rid, _), q in service._questions.items()
                if rid == child_id and q["answer"] is None]
    return result
