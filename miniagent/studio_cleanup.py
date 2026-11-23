"""Resolve concrete cleanup targets before changing Studio data. Called under Studio's lock."""
from __future__ import annotations

import hashlib
import json
import re

from . import studio_admin, studio_artifacts

DELETE_ROLE = "delete_library_role"
GROUPS = "delete_studio_groups"
APPLY = "apply_studio_cleanup"
BATCH = "studio_cleanup"
ORDER = ("delete_artifacts", "clear_chat", "delete_session", "delete_group", "purge_trash", DELETE_ROLE)


def pending(room):
    """Only the latest addressed reply may carry an actionable proposal."""
    for event in reversed(room.chat()):
        if event["kind"] in ("user_message", "room_reply", "chat_cleared"):
            return event.get("cleanup_proposal") if event["kind"] == "room_reply" else None
    return None


def _key(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _expand(studio, calls):
    steps = []
    anchors = {}
    role_teams = {}
    for call in calls:
        args = call["args"]
        if call["name"] != GROUPS:
            step = {"operation": DELETE_ROLE, **args} if call["name"] == DELETE_ROLE else dict(args)
            steps.append(step)
            continue
        ids = args["group_ids"]
        if not ids or not all(isinstance(gid, str) for gid in ids):
            raise ValueError("请选择具体群组。")
        for gid in dict.fromkeys(ids):
            owner = studio.group(gid)
            if owner.parent or owner.group.kind != "group":
                raise ValueError("整群清理需要群组自身的 ID。")
            if args["include_artifacts"]:
                for session in studio.store.sessions(owner):
                    steps.append({"operation":"delete_artifacts", "group_id":session.group.id, "all_files":True})
                    anchors["delete_artifacts", session.group.id] = gid
            steps.append({"operation":"delete_group", "group_id":gid})
            if args["include_roles"]:
                for role in owner.group.role_ids:
                    role_teams.setdefault(role, gid)

    removed = {s["group_id"] for s in steps if s["operation"] in ("delete_group","purge_trash")}
    for step in steps:
        if step["operation"] == "delete_session":
            session = studio.group(step["group_id"])
            if not session.parent and session.group.kind == "dm":
                removed.add(session.group.id)
    preserved = []
    for role, gid in sorted(role_teams.items()):
        if studio_admin.role_users(studio, role, excluding=removed):
            preserved.append(role)
        else:
            steps.append({"operation":DELETE_ROLE, "role":role})
            anchors[DELETE_ROLE, role] = gid
    return steps, anchors, preserved, removed


def _steps(studio, steps):
    # Collapse repeat calls and merge path subsets. A file is counted and removed once.
    unique = {}
    for args in steps:
        op = args["operation"]
        if op not in ORDER:
            raise ValueError("这组操作包含无法与清理合并的动作。")
        target = args["role"] if op == DELETE_ROLE else args["group_id"]
        if op == "delete_artifacts":
            paths = args.get("paths")
            if args.get("all_files") is True and paths is None:
                args = {"operation":op,"group_id":target,"all_files":True}
            elif isinstance(paths,list) and paths and not args.get("all_files"):
                paths = {studio_artifacts.relative_name(p) for p in paths}
                old = unique.get((op,target),{})
                if old.get("all_files"):
                    continue
                args = {"operation":op,"group_id":target,"paths":sorted(paths.union(old.get("paths",[])))}
            else:
                raise ValueError("请指定产物路径或选择全部产物。")
        elif set(args) != {"operation","role" if op == DELETE_ROLE else "group_id"}:
            raise ValueError("该清理操作的参数不匹配。")
        unique[op,target] = args

    # A group removal already removes its sessions and their transcripts (including trashed children).
    groups = {target for op, target in unique if op == "delete_group"}
    for op, target in list(unique):
        covered = target.partition("-s-")[0] in groups
        if ((op == "clear_chat" and (covered or ("delete_session",target) in unique))
                or (op == "delete_session" and covered)
                or (op == "purge_trash" and "-s-" in target and covered)):
            if op == "purge_trash":
                if not studio.store.trash_path(target).is_dir():
                    raise ValueError(f"废纸篓中找不到 {target}。")
            else:
                studio.group(target)
            del unique[op,target]
    return unique


def _resolve(studio, calls, posting):
    steps, anchors, omitted, removed = _expand(studio, calls)
    unique = _steps(studio, steps)
    named = []
    scope = []
    rows = []
    for op,target in sorted(unique,key=lambda item:(ORDER.index(item[0]),item[1])):
        args = unique[op,target]
        if op == DELETE_ROLE:
            role = studio.roles.get(target)
            users = studio_admin.role_users(studio, target, excluding=removed)
            if users:
                raise ValueError(f"角色 {target} 仍由{'、'.join(users)}使用。")
            label = f"删除角色「{role['name']}」 ({target})"
            identity = [role, studio.roles.history(target)]
            name = role["name"]
        elif op == "purge_trash":
            path = studio.store.trash_path(target)
            manifest = path/("session.json" if "-s-" in target else "group.json")
            if not manifest.is_file():
                raise ValueError(f"废纸篓中找不到 {target}。")
            name = json.loads(manifest.read_bytes())["name"]
            label = f"永久删除废纸篓条目「{name}」 ({target})"
            identity = [target,manifest.stat().st_mtime_ns]
        else:
            room = studio.group(target)
            if room.root.resolve() != room.root.absolute():
                raise ValueError("会话目录的位置发生了变化。")
            if op == "delete_group" and (room.parent or room.group.kind != "group"):
                raise ValueError("删除整个群必须指定群组本身。")
            affected = studio.store.sessions(room) if op == "delete_group" else [room]
            studio_admin._idle(studio,affected,posting=posting,allow_paused=op in ("delete_group","delete_session"))
            name = room.group.name
            if op == "delete_artifacts":
                files = studio_artifacts.publications(room)
                paths = sorted(files) if args.get("all_files") else args["paths"]
                missing = set(paths) - files.keys()
                if missing:
                    raise ValueError(f"「{name}」没有发布这些路径：" + "、".join(sorted(missing)))
                identity = [room.group.folder,[(p, sorted(files[p]["digests"]),sorted(files[p]["task_ids"])) for p in paths]]
                files_label = f"全部 {len(paths)} 个发布路径" if args.get("all_files") else "、".join(paths)
                label = f"清理「{name}」的{files_label}、历史版本及未修改的绑定副本 ({target})"
            else:
                identity = [(s.group.id, s.group.name, s.group.folder, sorted(s.group.role_ids),
                             sorted(r["id"] for r in s.tasks()),
                             (s.root/"chat.jsonl").stat().st_mtime_ns if s.group.id != posting else None)
                            for s in affected]
                if op == "delete_group":
                    trashed = sorted(p.parent.name for p in studio.store.trash_path(target).parent.glob(target+"-s-*/session.json"))
                    if trashed and any(("delete_artifacts", s.group.id) in unique for s in affected):
                        raise ValueError("此群有已移入废纸篓的子会话。如要一并清理绑定副本，请先恢复这些会话：" + "、".join(trashed))
                    identity.append(trashed)
                if op == "clear_chat":
                    label = f"清空「{name}」的聊天 ({target})"
                else:
                    verb = "删除整个群" if op == "delete_group" else "删除会话"
                    if op == "delete_session" and not room.parent and room.group.kind in ("assistant", "group"):
                        verb = "清除默认会话数据，保留群组及其他会话"
                    label = f"{verb}「{name}」 ({target}，含 {len(affected)} 个会话、{sum(len(s.tasks()) for s in affected)} 项任务)"
                    if op == "delete_group" and trashed:
                        label += f"，另含废纸篓中的 {len(trashed)} 个子会话"
                    if not any(("delete_artifacts", s.group.id) in unique for s in affected):
                        label += "，保留绑定文件夹中的副本"
        anchor = anchors.get((op,target), target)
        named.append((anchor, studio.group(anchor).group.name if anchor != target else name))
        rows.append({"args":args, "label":label, "name":name})
        scope.append(identity)
    digest = hashlib.sha256(_key([rows,scope,sorted(omitted)]).encode()).hexdigest()
    return rows, named, sorted(omitted), digest


def _named(studio, text, anchors):
    rooms = [(g.id, g.name) for g in studio.store.conversations()]
    roles = [(r["id"], r["name"]) for r in studio.roles.list()]
    for target, name in anchors:
        if re.search(r"(?<![a-zA-Z0-9_-])" + re.escape(target) + r"(?![a-zA-Z0-9_-])", text):
            continue
        peers = rooms if target.startswith("g-") else roles
        if not name or any(other != target and other_name == name for other, other_name in peers):
            return False
        # A shorter name inside another room's name does not identify that room.
        mentions = text
        for _, longer in peers:
            if name != longer and name in longer:
                mentions = mentions.replace(longer, "")
        if name not in mentions:
            return False
    return True


def run(studio, room, calls, text, previous=None, *, applying=False):
    """Return a chat receipt and the durable structured facts behind it."""
    rows, anchors, omitted, identity = _resolve(studio,calls,room.group.id)
    matches = previous and previous["id"] == identity
    preserved = ("\n\n其他群仍在使用，保留角色：" + "、".join(omitted)) if omitted else ""
    if not matches and (applying or previous or not _named(studio, text, anchors)):
        plan = {"id":identity,"calls":calls}
        listing = "\n".join("- " + row["label"] for row in rows)
        return ("请确认这次要清理的项目。\n\n" + listing + preserved +
                "\n\n确认后会按这个清单执行。也可以直接说要去掉哪一项。", {"cleanup_proposal":plan})

    results = []
    for row in rows:
        args = row["args"]
        try:
            if args["operation"] == DELETE_ROLE:
                result = studio_admin.remove_role(studio,args["role"])
                detail = f"已删除角色 {result['deleted_role']}（{row['name']}）。"
            else:
                result = studio_admin.manage(studio,posting=room.group.id,**args)
                if args["operation"] == "delete_group":
                    detail = f"已删除群「{row['name']}」（{args['group_id']}），共清理 {len(result['deleted_ids'])} 个会话。"
                else:
                    detail = studio_admin.receipt(result)
            results.append({"step":row["label"],"ok":True,"receipt":detail})
        except (KeyError, OSError, ValueError, RuntimeError) as exc:
            results.append({"step":row["label"],"ok":False,"error":str(exc)})
            break  # A later step can destroy the evidence or files needed to retry this one.
    skipped = [row["label"] for row in rows[len(results):]]
    lines = [r["receipt"] if r["ok"] else f"未完成：{r['step']}。{r['error']}" for r in results]
    if skipped:
        lines.append("尚未执行：" + "；".join(skipped))
    receipt = lines[0] if len(lines) == 1 else "\n".join("- " + line for line in lines)
    return receipt + preserved, {"cleanup_result":{"items":results,"not_run":skipped,"preserved_roles":omitted}}
