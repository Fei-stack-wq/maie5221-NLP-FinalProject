"""Project tool lifecycle records into the compact, public Studio timeline."""
from __future__ import annotations

import hashlib
import math
import re
from urllib.parse import urlsplit

from .project_data import validate_project_data

MAX_ACTIVITY = 100
_FILE_TOOLS = {"list_dir", "read_file", "read_lines", "read_source", "write_file", "edit_file", "append_file",
               "delete_file", "find_files", "search_files"}
_ERRORS = {"A_INVALID_ARGS": "工具参数无效", "A_UNKNOWN_TOOL": "工具不存在",
           "A_SANDBOX_DENY": "工作区拒绝访问", "A_HUMAN_DENIED": "未获执行许可",
           "A_TOOL_TIMEOUT": "工具执行超时", "A_SKIPPED": "本次调用未执行",
           "T_CANCEL": "执行已取消", "T_STORE": "无法保存调用身份",
           "O_EXEC_ERROR": "执行出错", "A_TOOL_ERROR": "工具返回错误"}


def target_for(tool, arguments):
    """Keep a project path, recipient or domain. Commands, code and URL queries stay private."""
    if tool == "http_get":
        try:
            value = urlsplit(str(arguments.get("url", ""))).hostname or ""
        except ValueError:
            return ""
    elif tool in {"delegate_task", "delegate"}:
        value = arguments.get("assignee", "")
    else:
        value = arguments.get("path", "")
    return _public_target(tool, value)


def _public_target(tool, value):
    if not isinstance(value, str):
        return ""
    if tool == "http_get":
        if not re.fullmatch(r"[\w.:-]{1,253}", value):
            return ""
    elif tool in {"delegate_task", "delegate"}:
        if not isinstance(value, str) or not re.fullmatch(r"[\w-]{1,64}", value):
            return ""
    elif tool in _FILE_TOOLS:
        if value == ".":
            return "."
        if (len(value) > 240 or
                not re.fullmatch(r"[\w ./()-]+", value) or
                any(not part or part.startswith(".") for part in value.split("/"))):
            return ""
    else:
        return ""
    try:
        validate_project_data("target.txt", value.encode(), reject_credentials=True)
    except ValueError:
        return ""
    return value


def _natural(value):
    return type(value) is int and value >= 0


_SCALAR = re.compile(r"exit_code=0\nstdout:\n(?P<value>-?(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,4})?|True|False|true|false|None|null)(?:\r?\n)?")
_CREATED = {"create_studio_role": ("role", r"[A-Za-z][A-Za-z0-9_-]{0,47}"),
            "create_studio_group": ("group", r"group-[0-9a-f]{32}")}


def created_name(tool, entity):
    """Read only the public display name from a host-issued creation receipt."""
    if tool not in _CREATED or not isinstance(entity, dict):
        return ""
    kind, pattern = _CREATED[tool]
    name, identifier = entity.get("name"), entity.get("id")
    if (entity.get("kind") != kind or not isinstance(identifier, str) or not re.fullmatch(pattern, identifier)
            or not isinstance(name, str) or not 0 < len(name) <= 100 or not name.strip() or not name.isprintable()
            or re.search(r"(?:password|token|secret|api[_-]?key|authorization)\s*[:=]", name, re.I)):
        return ""
    try:
        validate_project_data("name.txt", name.encode(), reject_credentials=True)
    except ValueError:
        return ""
    return name


def _scalar(event):
    preview = event.get("preview")
    if (event.get("complete_preview") is not True or type(event.get("exit_code")) is not int
            or event["exit_code"] != 0 or event.get("stderr_bytes", 0) != 0
            or event.get("timed_out") or event.get("cancelled") or event.get("truncated")
            or not isinstance(preview, str) or len(preview) > 40):
        return ""
    match = _SCALAR.fullmatch(preview)
    if not match:
        return ""
    size = len(preview) - match.start("value")  # the entire accepted value is ASCII
    return match["value"] if event.get("stdout_bytes", size) == size else ""


def _result(event, tool):
    """Human-readable results derived strictly from typed host metrics."""
    parts = []
    if _natural(event.get("returned_chars")) and tool in {"read_file", "read_source", "read_attachment"}:
        parts.append(f"已读取 {event['returned_chars']} 字符")
    if _natural(event.get("bytes")) and tool in {"write_file", "edit_file", "append_file", "http_get", "materialize_attachment"}:
        parts.append(f"{event['bytes']} 字节")
    if _natural(event.get("count")) and tool in {"list_dir", "find_files", "search_files", "search_sources", "list_attachments"}:
        parts.append(f"{event['count']} 项结果")
    if type(event.get("exit_code")) is int:
        parts.append(f"退出码 {event['exit_code']}")
    for key, title in (("stdout_bytes", "标准输出"), ("stderr_bytes", "错误输出")):
        if _natural(event.get(key)) and event[key]:
            parts.append(f"{title} {event[key]} 字节")
    if event.get("timed_out") is True:
        parts.append("已超时")
    return " · ".join(parts)


def project_activity(events, active=False, clipped=False, *, run_id=None, thread=None):
    """Join by attempt/action identity. Legacy receipts need no synthetic start."""
    thread = thread if isinstance(thread, dict) else {}
    recipient = thread.get("recipient")
    direct = bool(run_id and recipient and thread.get("kind") == "dm" and thread.get("members") == [recipient])
    calls = {}
    actors = {}
    ended = set()
    repeats = {}
    for event in events:
        kind = event.get("kind")
        task, attempt = event.get("task_id"), event.get("attempt_id")
        if kind in {"task_start", "task_registered"}:
            actors[(task, attempt)] = event.get("agent", "")
        elif kind == "task_end":
            ended.add((task, attempt))
        if kind not in {"tool_start", "tool_result"}:
            continue
        tool = event.get("operation") or event.get("tool")
        if not isinstance(tool, str) or not re.fullmatch(r"[a-zA-Z][\w-]{0,63}", tool):
            continue
        action = event.get("action_id")
        if not action and event.get("tool_call_id"):
            action = (event.get("step"), event["tool_call_id"])
        if not action and tool in {"system", "provider"}:
            continue
        if not action:
            # Older CLI logs predate call IDs and can contain repeated calls per step.
            fallback = (task, attempt, event.get("ts"), event.get("step"), tool)
            repeats[fallback] = repeats.get(fallback, 0) + 1
            action = (*fallback, repeats[fallback])
        identity = (task, attempt, action)
        if identity not in calls:
            uid = hashlib.sha256(repr(identity).encode()).hexdigest()[:24]
            calls[identity] = {"id": uid, "tool": tool, "role": "", "status": "running", "summary": ""}
        call = calls[identity]
        actor = event.get("agent") or actors.get((task, attempt))
        if isinstance(actor, str) and re.fullmatch(r"[\w-]{1,80}", actor):
            call["role"] = actor
        if kind == "tool_start":
            call["summary"] = _public_target(tool, event.get("target"))
            call["_start"] = event.get("ts")
            continue
        artifacts = event.get("artifacts")
        if not call["summary"] and isinstance(artifacts, list) and artifacts:
            call["summary"] = _public_target(tool, artifacts[0])
        call["status"] = "succeeded" if event.get("ok") is True else "failed"
        if event.get("cancelled") is True or event.get("error_class") == "T_CANCEL":
            call["status"] = "interrupted"
        # A group may run in single mode. Bind the projection to the stored room,
        # primary actor and root task instead of trusting mode or the tool alone.
        if (direct and call["status"] == "succeeded" and task == event.get("root_id") == run_id
                and event.get("level") == "goal" and not event.get("parent_task_id")
                and event.get("agent") == recipient and tool == event.get("tool") == call["tool"]):
            if tool in {"run_python", "run_shell"}:
                call["summary"] = _scalar(event)
            elif thread.get("superagent") is True and tool in _CREATED:
                call["summary"] = created_name(tool, event.get("created_entity"))
        call["output"] = _result(event, call["tool"])
        if call["status"] != "succeeded":
            call["error"] = _ERRORS.get(event.get("error_class"), "工具调用未成功")
        duration = event.get("elapsed_ms")
        start, finish = call.get("_start"), event.get("ts")
        if not isinstance(duration, (int, float)) and type(start) in (int, float) and type(finish) in (int, float):
            duration = (finish - start) * 1000
        if type(duration) in (float, int) and math.isfinite(duration) and duration >= 0:
            call["duration_ms"] = round(duration)
    visible = list(calls.items())[-MAX_ACTIVITY:]
    for (task, attempt, _), call in visible:
        call.pop("_start", None)
        if call["status"] == "running" and (not active or (task, attempt) in ended):
            call.update(status="interrupted", error="没有记录到工具完成回执")
    return {"calls": [call for _, call in visible], "truncated": clipped or len(calls) > len(visible)}
