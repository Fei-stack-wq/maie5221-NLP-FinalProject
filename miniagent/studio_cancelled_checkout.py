"""Continue an internal group project from input bytes and published handoff receipts."""
import hashlib
import json
import re

from .project_data import validate_project_data
from .studio_conversations import SnapshotError
from .studio_workspace import _project_path


def published_edits(events, thread, run_id):
    assignments = {}
    for row in events:
        role, task = row.get("role"), row.get("task_id")
        if row.get("kind") == "assignment":
            assignments[task] = row.get("recipient")
        if row.get("published") is not True or role not in thread["members"] or not task:
            continue
        member = row.get("kind") == "member_report" and assignments.get(task) == role
        direct = row.get("kind") == "direct_delivery" and task == run_id and role == thread["recipient"]
        if not (member or direct):
            continue
        handoff = row.get("handoff") or {}
        if handoff.get("task_id") == task and handoff.get("role") == role and handoff.get("changed_files"):
            yield row


def restore(service, source, thread, target, extensions):
    from .studio_service import _read_file
    if thread["kind"] != "group" or service._meta(source).get("state") != "cancelled":
        raise SnapshotError("只能从本群取消轮的公开交接继续。")
    expected, links = {}, {}
    for filename in ("inherited-inputs.json", "dispatch-inputs.json"):
        data, clipped = _read_file(source, filename, 4 * 1024**2, missing=True)
        if clipped:
            raise SnapshotError("取消轮的输入清单不完整。")
        if data:
            expected.update(json.loads(data))
    rows = published_edits(service.room_events(source.name), thread, source.name)
    for row in rows:
        handoff = row["handoff"]
        changes = {item["path"]: item for item in handoff["changed_files"]}
        artifacts = {item["project_path"]: item for item in row.get("artifacts", [])}
        clipped = handoff.get("changes_total", len(changes)) > len(changes)
        if clipped:
            # A clipped diff may omit deletions. The immutable full snapshot
            # determines which previous names survived, never which new files to trust.
            ref = next((a.get("path", "") for a in artifacts.values()), "")
            snapshot = ref.split("/", 2)
            if len(snapshot) != 3 or snapshot[0] != "handoffs" or not re.fullmatch("[0-9a-f]{32}", snapshot[1]):
                raise SnapshotError("取消轮的公开文件变更清单不完整。")
            manifest, cut = _read_file(source, f"handoffs/{snapshot[1]}/manifest.json", 4 * 1024**2)
            if cut:
                raise SnapshotError("公开交接清单不完整。")
            surviving = json.loads(manifest)
            expected = {name: sha for name, sha in expected.items() if name in surviving}
        for name, change in changes.items():
            if change["change"] == "deleted":
                expected.pop(name, None)
                links.pop(name, None)
            elif name not in artifacts or change.get("sha256") != artifacts[name].get("sha256"):
                raise SnapshotError("取消轮的已发布文件缺少匹配的交接回执。")
        for name, artifact in artifacts.items():
            if not clipped and name not in changes:
                continue
            if not _project_path(name, extensions) or not re.fullmatch(
                    "handoffs/[a-f0-9]{32}/" + re.escape(name), artifact.get("path", "")):
                raise SnapshotError("取消轮的公开交接文件路径无效。")
            links[name] = artifact["path"]
            expected[name] = artifact["sha256"]
    total = 0
    if len(expected) > 1000:
        raise SnapshotError("公开交接项目超过目录项上限。")
    for name, sha in expected.items():
        if not _project_path(name, extensions):
            raise SnapshotError("取消轮的输入文件路径无效。")
        if name in links:
            data, clipped = service._artifact_data(source.name, links[name], 8 * 1024**2)
        else:
            data, clipped = _read_file(source, f"agents/{thread['recipient']}/{name}", 8 * 1024**2)
        total += len(data)
        if clipped or total > 20 * 1024**2 or hashlib.sha256(data).hexdigest() != sha:
            raise SnapshotError("取消轮的文件与其公开交接或输入回执不一致。")
        validate_project_data(name, data, secret=service.settings.api_key, reject_credentials=True)
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("xb") as out:
            out.write(data)
    return expected
