"""Persistent project conversations with directed replies and lead-owned team runs.

A successful edit publishes a new project workspace. Questions borrow a private
read-only copy of the last published workspace. A run can therefore be the
conversation head without being the current project version.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .project_data import PROJECT_CACHE_DIRS, SQLITE_SIDECARS, supported_project_file, validate_project_data

ROLE_LABELS = {"worker": "通用助手", "researcher": "研究员", "writer": "写作者", "reviewer": "审阅者"}

ROLE_DUTIES = {
    "worker": "通用执行、整理和一次性任务",
    "researcher": "查找原始资料、调研并整理来源",
    "writer": "撰写或编辑文章、说明和报告",
    "reviewer": "独立检查交付、核实结果并提出修改意见",
}


_ROLE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,47}\Z")


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ROLE_ID.fullmatch(value))


def validate_conversation(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("会话参数须为对象。")
    intent, recipient = value.get("intent"), value.get("recipient")
    if intent not in ("question", "edit") or not _valid_id(recipient):
        raise ValueError("请指定消息用途与有效的回复角色。")
    if "parent_run_id" in value:
        required = {"parent_run_id", "intent", "recipient"}
        parent = value["parent_run_id"]
        if (not required <= value.keys() <= required | {"inherit_files", "recover_failed", "recover_task_id"}
                or type(parent) is not str or not parent or len(parent) > 128
                or any(type(value[k]) is not bool for k in ("inherit_files", "recover_failed") if k in value)):
            raise ValueError("后续消息必须指向上一轮，成员与职责由原会话继承。")
        if "recover_task_id" in value and (
                not value.get("recover_failed") or not isinstance(value["recover_task_id"], str)
                or not re.fullmatch(r"task-[0-9a-f]{32}", value["recover_task_id"])):
            raise ValueError("请选择本轮失败后要接续的成员任务。")
        if (value.get("recover_failed") and (not value.get("inherit_files", True) or intent != "edit")
                or intent == "question" and not value.get("inherit_files", True)):
            raise ValueError("只可在修改项目时选择新文件或继续失败版本。")
        return dict(value)
    required = {"kind", "members", "recipient", "intent"}
    if not required <= value.keys() <= required | {"profiles"}:
        raise ValueError("新会话需要类型、成员、回复角色和消息用途。")
    kind, members = value["kind"], value["members"]
    if (kind not in ("dm", "group") or type(members) is not list or len(members) > 16
            or not all(_valid_id(member) for member in members)
            or len({member.lower() for member in members}) != len(members) or recipient not in members):
        raise ValueError("会话成员或回复角色无效。")
    if kind == "dm" and len(members) != 1:
        raise ValueError("私聊只能指定一位角色。")
    profiles = value.get("profiles", [])
    if not isinstance(profiles, list) or len(profiles) > len(members):
        raise ValueError("角色资料须对应群内成员。")
    profiles_by_id = {}
    for profile in profiles:
        if (not isinstance(profile, dict) or set(profile) != {"id", "name", "prompt"}
                or not _valid_id(profile["id"]) or profile["id"] not in members
                or profile["id"] in profiles_by_id
                or any(type(profile[k]) is not str or not profile[k].strip() or len(profile[k]) > limit
                       for k, limit in (("name", 100), ("prompt", 4000)))):
            raise ValueError("请为每位自定义角色填写唯一 ID、名称和职责。")
        profiles_by_id[profile["id"]] = dict(profile)
    if set(members) - ROLE_LABELS.keys() - profiles_by_id.keys():
        raise ValueError("未提供自定义成员的角色资料。")
    result = {"kind": kind, "members": list(members), "recipient": recipient, "intent": intent}
    if profiles:
        result["profiles"] = list(profiles_by_id.values())
    return result


def thread_info(run_id: str, meta: dict) -> dict | None:
    if meta.get("mode") in ("demo", "single", "team", "room") and "thread" in meta:
        return meta["thread"]
    if meta.get("mode") not in ("demo", "single"):
        return None
    return {"id": run_id, "kind": "dm", "members": ["worker"], "recipient": "worker",
            "intent": "edit", "parent_run_id": None, "ordinal": 1, "base_run_id": None}


def member_profiles(thread: dict) -> dict[str, dict]:
    custom = {p["id"]: p for p in thread.get("profiles", [])}
    return {role: custom.get(role, {"id": role, "name": ROLE_LABELS.get(role, role), "prompt": ""})
            for role in thread["members"]}


def addressed_members(text: str, thread: dict) -> list[str]:
    """Only the address at the beginning of a group message selects its recipients."""
    profiles = member_profiles(thread)
    ids = {key.casefold(): key for key in profiles}
    aliases: dict[str, list[str]] = {}
    for role, profile in profiles.items():
        aliases.setdefault(profile["name"].casefold(), []).append(role)
    names = sorted({*profiles, *(p["name"] for p in profiles.values())}, key=len, reverse=True)
    if not names:
        return []
    address = "(?:" + "|".join(map(re.escape, names)) + ")"
    token = re.compile(r"@" + address + r"(?=$|[\s,:：，、;；!?！？。])", re.IGNORECASE)
    bare = re.compile(address + r"(?=\s*[，,:：])", re.IGNORECASE)
    rest = text.lstrip()
    recipients = []
    while rest:
        mention = rest.startswith("@")
        match = (token if mention else bare).match(rest)
        if not match:
            if mention:
                raise ValueError("点名未匹配本群成员。请在消息开头使用 @角色ID 和空格。")
            break
        name = match.group()[int(mention):].casefold()
        roles = [ids[name]] if name in ids else aliases.get(name, [])
        if len(roles) != 1:
            raise ValueError("群内存在同名角色，请用 @角色ID 指定：" + "、".join(roles))
        if roles[0] not in recipients:
            recipients.append(roles[0])
        rest = rest[match.end():].lstrip(" \t\r\n,:：，、;；")
        if not rest.startswith("@"):
            break
    return recipients


def project_context(thread: dict, history: list[dict]) -> str:
    if thread.get("superagent"):
        lines = ["统一工作室对话。下面是历史消息、当前项目的派工回执和可用角色。当前用户要求在下一条消息中。"]
    else:
        role = thread["recipient"]
        profile = member_profiles(thread)[role]
        lines = [f"项目会话。你的角色是{profile['name']}（{role}）。", profile["prompt"],
                 "以下是历史公开消息，供理解项目与既有决定。当前用户要求在下一条消息中。",
                 "本轮是只读问答，可用 find_files、search_files、list_dir、read_lines、read_source 和 read_file 核实项目，再直接回答。"
                 if thread["intent"] == "question" else
                 "请检查项目工作区并按当前用户要求完成交付。"]
    if thread.get("recovery_source"):
        source = thread["recovery_source"]
        lines.append(f"本轮从上一轮受阻成员 {source['role']} 的项目检查点恢复。已有文件和数据可以核验，上一轮验收仍为失败。")
    if thread.get("inherit_files") is False:
        lines.append("本轮是同一团队的新项目，使用独立文件起点。前一题的目标与产物只作历史参考。")
    if not history:
        return "\n".join(lines)
    lines.append("项目的初始要求：" + history[0]["user"])
    # Publish full replies separately. The replay slice must always leave room
    # for the active request, even after many multi-page conversations.
    remaining = 96 * 1024
    recent = []
    for turn in reversed(history):
        answer = turn["answer"]
        excerpt = answer if len(answer) <= 5000 else answer[:3000] + "\n[…回复中段已省略…]\n" + answer[-2000:]
        entry = json.dumps({"用户": turn["user"], "回复者": turn["recipient"],
                            "执行状态": turn["status"], "历史回复": excerpt,
                            "回复摘录": turn["answer_truncated"] or len(answer) > 5000}, ensure_ascii=False)
        decisions = [{"role": e["role"], "kind": e["kind"], "text": e["text"][:1600]}
                     for e in turn.get("events", []) if e["kind"] in {"member_report", "human_answer"}]
        if thread.get("superagent"):
            decisions.extend({"已创建": e["entity"]} for e in turn.get("events", [])
                             if e["kind"] == "studio_entity" and isinstance(e.get("entity"), dict))
            for dispatch in turn.get("dispatches", [])[-8:]:
                decisions.append({"派工": dispatch["objective"][:600], "收件人": dispatch["target_name"],
                                  "目标 ID": dispatch["target_id"], "状态": dispatch["status"],
                                  "答复节选": dispatch["answer"][:1600], "错误": dispatch.get("error", ""),
                                  "交付引用": [file["ref"] for file in dispatch["artifacts"][:24]]})
        if decisions:
            entry += "\n" + json.dumps(decisions[-12:], ensure_ascii=False)
        cost = len(entry.encode("utf-8")) + 1
        if cost > remaining:
            break
        recent.append(entry)
        remaining -= cost
    omitted = len(history) - len(recent)
    if omitted:
        lines.append(f"以下省略了较早的 {omitted} 轮消息。完整记录保存在项目会话中；需要旧细节时请向用户核对。")
    lines.extend(reversed(recent))
    return "\n".join(lines)


class SnapshotError(ValueError):
    """Only a complete, validated set of supported project files may be used."""


class UnsupportedProjectFile(SnapshotError):
    """Regular file that the current project format cannot publish."""


def snapshot_location(relative: Path, secret: str) -> str:
    """Only quote bounded, printable project-relative names without credential material."""
    value = relative.as_posix()
    if (relative.is_absolute() or ".." in relative.parts or len(value) > 240 or not value.isprintable() or not re.fullmatch(r"[\w ./()（）-]+", value)
            or re.search(r"(?i)(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)", value)):
        return "（文件名已隐藏）"
    try:
        validate_project_data("path.txt", value.encode(), secret=secret, reject_credentials=True)
    except (ValueError, UnicodeError):
        return "（文件名已隐藏）"
    return "「" + value + "」"


def copy_project(source: Path, recipient: str, target: Path | None, extensions: set[str], *,
                 secret: str = "", manifest: dict | None = None) -> None:
    """Validate every file. Optionally copy to private staging or record its digest."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    total_bytes, entries = 0, 0
    unsupported = []

    def unsupported_error():
        # Prefer shorter paths so a deep cache tree cannot hide a top-level
        # deliverable. Only a bounded list enters a tool reply.
        paths = sorted(unsupported, key=lambda p: (len(p.parts), p.as_posix()))
        listed = "、".join(snapshot_location(p, secret) for p in paths[:12])
        remaining = f"（另有 {len(paths) - 12} 项）" if len(paths) > 12 else ""
        return UnsupportedProjectFile("项目含尚不支持交接的文件类型" + listed + remaining
                                      + "。公开资料可用 http_get 保留文本来源；本轮自行产生的原始下载和工具缓存可移至"
                                      "项目外的系统临时目录。若原件本身是约定交付，请保留并报告格式限制。")

    def visit(directory: int, relative: Path, depth: int) -> None:
        nonlocal total_bytes, entries
        if depth > 64:
            raise SnapshotError("项目目录层级过深。")
        if target is not None:
            (target / relative).mkdir(parents=True, exist_ok=True)
        with os.scandir(directory) as children:
            for child in children:
                if child.name.startswith(".") or (child.name in PROJECT_CACHE_DIRS and child.is_dir(follow_symlinks=False)):
                    continue
                name = relative / child.name
                if child.name.lower().endswith(SQLITE_SIDECARS):
                    raise SnapshotError("数据库仍有日志文件" + snapshot_location(name, secret)
                                        + "，请关闭数据库并完成 checkpoint 后交接。")
                entries += 1
                if entries > 1000:
                    raise SnapshotError("项目快照超过 1000 个目录项。")
                info = child.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    fd = os.open(child.name, flags, dir_fd=directory)
                    try:
                        visit(fd, relative / child.name, depth + 1)
                    finally:
                        os.close(fd)
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SnapshotError("项目含尚不支持交接的链接或特殊文件" + snapshot_location(name, secret)
                                        + "。请核实项目中的该项，交接需要普通文件。")
                if not supported_project_file(child.name, extensions):
                    # A single research download can leave several cache files.
                    # Collect every blocking name under the existing entry limit
                    # so one repair can address them together. Never copy them.
                    unsupported.append(name)
                    if target is not None or manifest is not None:
                        raise unsupported_error()
                    continue
                fd = os.open(child.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or not os.path.samestat(opened, info):
                        raise SnapshotError("读取项目时文件已变化" + snapshot_location(name, secret) + "。")
                    size_limit = min(8 * 1024 ** 2, 20 * 1024 ** 2 - total_bytes)
                    if opened.st_size > size_limit:
                        raise SnapshotError("项目快照超过单文件 8 MiB 或总量 20 MiB，当前文件" + snapshot_location(name, secret) + "。")
                    data = stream.read(size_limit + 1)
                    after = os.fstat(stream.fileno())
                    if (len(data) != opened.st_size or after.st_size != opened.st_size
                            or after.st_mtime_ns != opened.st_mtime_ns):
                        raise SnapshotError("读取项目时文件已变化" + snapshot_location(name, secret) + "。")
                try:
                    validate_project_data(name.as_posix(), data, secret=secret, reject_credentials=True)
                except ValueError as exc:
                    raise SnapshotError(str(exc) + snapshot_location(name, secret)) from None
                if target is not None:
                    (target / name).write_bytes(data)
                if manifest is not None:
                    manifest[name.as_posix()] = hashlib.sha256(data).hexdigest()
                total_bytes += len(data)

    descriptors = []
    try:
        descriptors.append(os.open(source, flags))
        for name in ("agents", recipient):
            descriptors.append(os.open(name, flags, dir_fd=descriptors[-1]))
        visit(descriptors[-1], Path(), 0)
        if unsupported:
            raise unsupported_error()
    except OSError as exc:
        raise SnapshotError("读取原项目失败，无权读取的文件或链接无法交接。") from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
