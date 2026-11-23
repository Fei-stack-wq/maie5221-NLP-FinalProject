"""Studio-wide management addressed to concrete conversations, available to the assistant."""
import json
import shutil
from collections import Counter
from pathlib import Path

from . import studio_artifacts
from .schemas import ToolSpec


STRING = {"type":"string"}
INSPECT = ToolSpec(name="inspect_studio", description=(
    "Read live Studio-wide facts on demand. overview returns rooms, roles, model settings and task counts; "
    "chat reads the named conversation newest first; artifacts lists published paths and task revisions; "
    "tasks lists global or room tasks; file reads a current file or its task snapshot; search finds matching chat, tasks and file paths across rooms; trash lists restorable entries. Page with offset (bytes for file)."),
    parameters={"type":"object","additionalProperties":False,"properties":{
        "view":{"type":"string","enum":["overview","chat","artifacts","tasks","trash","file","search"]},
        "group_id":STRING,"path":STRING,"task_id":STRING,"query":STRING,"offset":{"type":"integer","minimum":0}}, "required":["view"]})

OPERATIONS = ("delete_artifacts","clear_chat","delete_session","delete_group","restore_trash","purge_trash")
MANAGE = ToolSpec(name="manage_studio_data", description=(
    "Apply the user's specific Studio data request. Use the exact conversation ID. clear_chat keeps tasks and files. "
    "delete_artifacts takes published paths OR all_files:true and removes current and historical copies, including "
    "unchanged synced copies in the bound folder. delete_session permanently removes one DM or child session; "
    "for a team's default session it resets just that session. delete_group requires an explicit whole-team deletion "
    "and includes all child sessions. Session/group deletion leaves user-owned local folders intact. "
    "restore_trash or purge_trash operate on the exact existing trash entry. Inspect first if the target is unclear."),
    parameters={"type":"object","additionalProperties":False,"properties":{
        "operation":{"type":"string","enum":list(OPERATIONS)},"group_id":STRING,
        "paths":{"type":"array","items":STRING},"all_files":{"type":"boolean"}},
        "required":["operation","group_id"]})


def inspect(studio, view, group_id=None, offset=0, path=None, task_id=None, query=None):
    if type(offset) is not int or offset < 0:
        raise ValueError("Invalid offset")
    room = studio.group(group_id) if group_id is not None else None
    base = {}
    if view == "overview":
        from .settings_api import model_settings
        groups = studio.conversations()
        rows = [{k:r[k] for k in ("id","name","kind","team_id","team_name","session_name","lead","archived","pinned",
                                   "running","waiting_for_you","tasks","workspace_path")} for r in groups]
        base = {"models":model_settings(studio.settings),"has_api_key":bool(studio.settings.api_key),
                "task_states":dict(Counter(r["status"] for g in groups for r in studio.tasks(g["id"]))),
                "roles":[{"id":r["id"],"name":r["name"]} for r in studio.roles.list()]}
    elif view == "tasks":
        rooms = [room] if room else [studio.group(g.id) for g in studio.store.conversations()]
        rows = sorted(({"id":r["id"],"group_id":s.group.id,"status":r["status"],"primary":r["primary"],
                        "request":r.get("text","")[:500],"created_at":r.get("created_at",0)}
                       for s in rooms for r in s.tasks()),key=lambda r:r["created_at"],reverse=True)
    elif view == "chat" and room:
        rows = [{k:(v[:1600] if isinstance(v,str) else v) for k,v in e.items()
                 if k in ("seq","time","kind","agent","addressed","task_id","assignee","text","question","task")}
                for e in reversed(room.chat()) if e["kind"] not in ("chat_cleared","history_updated")]
    elif view == "artifacts" and room:
        rows = [{"path":p,"versions":v["versions"],"task_ids":sorted(v["task_ids"])}
                for p,v in sorted(studio_artifacts.publications(room).items())]
    elif view == "file" and room:
        name = studio_artifacts.relative_name(path)
        if task_id:
            from .workbench import published_bytes
            owner, _ = studio.find_task(task_id)
            if owner.group.id != room.group.id:
                raise ValueError("Task does not belong to the selected session")
            data = published_bytes(room,task_id,name)
            if data is None:
                raise ValueError("Published revision unavailable")
            block, size = data[offset:offset+8192],len(data)
        else:
            with studio.read_file(room.group.id,name).open("rb") as stream:
                stream.seek(0,2)
                size = stream.tell()
                stream.seek(offset)
                block = stream.read(8192)
        return {"path":name,"group_id":room.group.id,"text":block.decode("utf-8","replace"),"size":size,
                "next_offset":offset+len(block) if offset+len(block)<size else None}
    elif view == "search" and isinstance(query,str) and query.strip():
        needle = query.strip().casefold()
        rooms = [room] if room else [studio.group(g.id) for g in studio.store.conversations()]
        rows = []
        for target in rooms:
            gid = target.group.id
            for rec in reversed(target.tasks()):
                if needle in str(rec.get("text","")).casefold():
                    rows.append({"group_id":gid,"task_id":rec["id"],"request":rec["text"][:400]})
            for event in reversed(target.chat()):
                body = event.get("text") or event.get("question") or event.get("task")
                if isinstance(body,str) and needle in body.casefold():
                    rows.append({"group_id":gid,"seq":event["seq"],"text":body[:400]})
            for name in studio_artifacts.publications(target):
                if needle in name.casefold():
                    rows.append({"group_id":gid,"path":name})
    elif view == "trash":
        rows = []
        for p in sorted((studio.store.root.parent/"trash").glob("g-*")):
            root = studio.store.trash_path(p.name)
            meta = root / ("session.json" if "-s-" in p.name else "group.json")
            if meta.is_file():
                data = json.loads(meta.read_bytes())
                rows.append({"id":p.name,"name":data["name"],"kind":data.get("kind","session")})
    else:
        raise ValueError("Unknown view or missing conversation ID")
    page = rows[offset:offset+25]
    return {**base,"view":view,"rows":page,"total":len(rows),
            "next_offset":offset+len(page) if offset+len(page)<len(rows) else None}


def _idle(studio, rooms, *, posting=None, allow_paused=False):
    for room in rooms:
        gid = room.group.id
        blocked = {"running","queued"} if not allow_paused else {"running"}
        if not allow_paused:
            blocked.add("paused")
        if (gid in studio._running or (room.turn_lock.locked() and gid != posting)
                or any(r["status"] in blocked for r in room.tasks())):
            raise ValueError(f"「{room.group.name}」有未结束的执行或对话，请先处理该任务。")


def _forget_scopes(studio, ids):
    scopes = {"group:"+i for i in ids}
    with studio.roles.lock:
        for p in studio.roles.root.glob("*/knowledge.jsonl"):
            if p.resolve() != p.absolute():
                raise ValueError("Unexpected knowledge location")
            rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
            remaining = [r for r in rows if r["scope"] not in scopes]
            if len(remaining) != len(rows):
                with studio_artifacts.replacement(p) as tmp:
                    tmp.write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in remaining),encoding="utf-8")


def _forget_receipts(studio, ids):
    for g in studio.store.groups():
        if g.kind == "assistant":
            source = studio.group(g.id)
            source.revise_chat(lambda e: None if e.get("target_group_id") in ids and e["kind"] in
                               ("assistant_group_created","dispatch_sent","dispatch_finished") else e)


def _reset_default(studio, room):
    # The original team ID is also its default conversation URL. Child session
    # manifests and the shared roster belong to the team rather than this transcript.
    marker = room.root / ".resetting-default"
    marker.touch()
    room.clear_chat(keep_task_inputs=False)
    for p in room.root.iterdir():
        if p.name in ("group.json","sessions","chat.jsonl",".resetting-default"):
            continue
        if p.is_symlink() or not p.is_dir():
            p.unlink()
        else:
            shutil.rmtree(p)
    room.group.folder = ""
    room.select_project()
    studio.store.update_group(room)
    marker.unlink()


def _shared_files(studio, room):
    """Do not unlink an external path also tracked by another conversation."""
    shared = set()
    if room.group.folder:
        folder = Path(room.group.folder)
        for g in studio.store.conversations():
            if g.id == room.group.id or not g.folder:
                continue
            other = studio.group(g.id)
            if other.folder_state.is_file():
                state = json.loads(other.folder_state.read_bytes())
                if state.get("folder") == g.folder:
                    for name in state.get("files",{}):
                        absolute = Path(g.folder) / name
                        if absolute.is_relative_to(folder):
                            shared.add(absolute.relative_to(folder).as_posix())
    return shared


def recover(studio, room):
    if (room.root/".resetting-default").is_file():
        _reset_default(studio,room)
    pending = room.root/".pending-artifact-delete.json"
    if pending.is_file():
        with studio._folder_sync_lock:
            result = studio_artifacts.finish_deletion(room)
        _scrub_cards(studio, room, set(result["deleted_paths"]))
        pending.unlink()
        return result


def remove_role(studio, role):
    with studio._lock, studio.roles.lock:
        studio.roles.get(role)
        users = [g.name for g in studio.store.groups() if role in g.role_ids or any(m.id == role for m in g.members)]
        for manifest in (studio.store.root.parent/"trash").glob("g-*/group.json"):
            data = json.loads(manifest.read_bytes())
            if role in data.get("role_ids",[]):
                users.append(data["name"]+"（废纸篓）")
        if users:
            raise ValueError("请先移除或替换以下会话中的角色：" + "、".join(users))
        path = studio.roles.path(role)
        if path.resolve() != path.absolute():
            raise ValueError("Unexpected role location")
        shutil.rmtree(path)
        return {"deleted_role":role}


def _purge_entry(studio, group_id):
    root = studio.store.trash_path(group_id)
    if not root.is_dir():
        raise ValueError("未在废纸篓找到该会话。")
    ids = {group_id, *(group_id+"-"+p.parent.name for p in (root/"sessions").glob("s-*/session.json"))}
    # A child may already have been trashed independently before its parent.
    descendants = [p.parent.name for p in root.parent.glob(group_id+"-s-*/session.json")]
    ids.update(descendants)
    _forget_scopes(studio,ids)
    _forget_receipts(studio,ids)
    for child in descendants:
        studio.store.purge_trash(child)
    studio.store.purge_trash(group_id)
    return sorted(ids)


def manage(studio, operation, group_id, paths=None, all_files=False, *, posting=None):
    if operation not in OPERATIONS or (operation != "delete_artifacts" and (paths is not None or all_files)):
        raise ValueError("Unknown operation or unexpected file arguments")
    with studio._lock:
        if operation == "restore_trash":
            room = studio.store.restore(group_id)
            return {"operation":operation,"id":room.group.id,"name":room.group.name}
        if operation == "purge_trash":
            return {"operation":operation,"deleted_ids":_purge_entry(studio,group_id)}
        room = studio.group(group_id)
        if room.root.resolve() != room.root.absolute():
            raise ValueError("Unexpected conversation location")
        if operation == "delete_group" and (room.parent or room.group.kind != "group"):
            raise ValueError("删除整个群需要群组自身的 ID。")
        affected = studio.store.sessions(room) if operation == "delete_group" else [room]
        _idle(studio, affected, posting=posting, allow_paused=operation in ("delete_session","delete_group"))
        if operation == "clear_chat":
            return {"operation":operation,"session_id":group_id,"cleared_events":room.clear_chat()}
        if operation == "delete_artifacts":
            recovered = recover(studio,room)
            if recovered and (all_files and not studio_artifacts.publications(room)
                              or isinstance(paths,list) and paths and set(recovered["deleted_paths"]) == set(paths)):
                return recovered
            studio_artifacts.plan_deletion(room, paths, all_files, shared=_shared_files(studio,room))
            return recover(studio,room)
        ids = {s.group.id for s in affected}
        if operation == "delete_session" and not room.parent and room.group.kind in ("group","assistant"):
            _forget_scopes(studio, ids)
            _forget_receipts(studio, ids)
            _reset_default(studio,room)
            return {"operation":operation,"reset_default":group_id,"preserved_team":room.team.id}
        studio.store.trash(room)
        return {"operation":operation,"deleted_ids":_purge_entry(studio,group_id)}


def _scrub_cards(studio, room, names):
    if not names:
        return
    # Retain chat prose but remove structured file links both here and in dispatch receipts.
    for g in studio.store.conversations():
        if g.id != room.group.id and g.kind != "assistant":
            continue
        source = studio.store.get(g.id)
        def prune(event):
            key = ("changes" if source is room else None)
            if event.get("target_group_id") == room.group.id:
                key = "artifacts"
            if key and event.get(key):
                event[key] = [c for c in event[key] if c.get("path") not in names]
            return event
        source.revise_chat(prune)


def receipt(result):
    op = result["operation"]
    if op == "delete_artifacts":
        names = result["deleted_paths"]
        preview = "、".join(names[:12])
        kept = "、".join(dict.fromkeys(r["path"] for r in result["preserved"]))
        return (f"已清理会话 {result['session_id']} 的 {len(names)} 个产物及历史快照（{preview or '无'}）。"
                f"绑定文件夹删除了 {len(result['workspace_deleted'])} 个副本。"
                + (f"另有副本因已修改、共享或无法核实而保留：{kept}。" if kept else ""))
    if op == "clear_chat":
        return f"已清空会话 {result['session_id']} 的聊天记录。任务和项目文件仍可查看。"
    if op == "restore_trash":
        return f"已从废纸篓恢复「{result['name']}」（{result['id']}）。"
    if "reset_default" in result:
        return f"已彻底清除默认会话的数据（{result['reset_default']}），群组和其他会话仍可使用。"
    return "已彻底删除：" + "、".join(result["deleted_ids"]) + "。"
