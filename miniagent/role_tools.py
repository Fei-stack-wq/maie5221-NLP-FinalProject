"""Role-scoped knowledge tools for the Studio's existing ToolRegistry.

Hermes Agent's memory tools and on-demand skill loading informed the interface:
https://github.com/NousResearch/hermes-agent/blob/main/tools/memory_tool.py
https://github.com/NousResearch/hermes-agent/blob/main/tools/skills_tool.py
This implementation uses Studio's versioned store and user approval workflow.
"""
from __future__ import annotations

import json

from .project_data import validate_project_data
from .schemas import ToolResult
from .studio_role_memory import RoleMemoryNotFound
from .tools import Tool, ToolRegistry

ROLE_READ_TOOLS = frozenset({"recall_role_knowledge", "read_role_knowledge"})
ROLE_TOOLS = ROLE_READ_TOOLS | {"propose_role_knowledge"}


def attach_role_tools(registry, store, role, project, run_id, *, read_only=False, redactor=None, secret=""):
    """Return a separately bound registry. An existing role binding is replaced."""
    bound = ToolRegistry([registry.get(name) for name in registry.names() if name not in ROLE_TOOLS])

    def clean(value):
        text = redactor(value) if redactor else value
        if secret:
            text = text.replace(secret, "[REDACTED]")
        try:
            validate_project_data("knowledge.txt", text.encode("utf-8"), reject_credentials=True)
        except (UnicodeError, ValueError):
            return "[该字段含凭据或无效文本，已隐藏]"
        return text

    def visible(entry):
        return (entry["role"] == role and entry["status"] == "active"
                and entry["active_revision"] == entry["revision"] and in_scope(entry))

    def in_scope(entry):
        return (entry["scope"] == "role" or
                (entry["scope"] == "project" and bool(project) and entry["project"] == project))

    def active_entry(entry_id):
        # get(role, id) may point to an unapproved candidate revision.
        try:
            row = store.get(role, entry_id)
            if row["active_revision"] is None or not in_scope(row):
                return None
            if row["active_revision"] != row["revision"]:
                row = store.get(role, entry_id, revision=row["active_revision"])
            return row if visible(row) else None
        except RoleMemoryNotFound:
            return None

    def card(entry):
        return {"entry_id": entry["id"], "revision": entry["revision"], "kind": entry["kind"],
                "scope": entry["scope"], "title": clean(entry["title"])}

    def recall(_ctx, args):
        query = args.get("query", "").strip()
        limit = args.get("limit", 6)
        kind = args.get("kind")
        if len(query) > 256 or not 1 <= limit <= 12 or kind not in {None, "memory", "skill"}:
            return ToolResult.failure("query 最长 256 字，limit 须为 1 至 12，kind 可选 memory 或 skill。")
        entries = store.recall(role, project=project, query=query, kind=kind, limit=limit)
        rows = []
        for entry in entries:
            if not visible(entry):
                continue
            text = clean(entry["text"])
            pos = text.casefold().find(query.casefold()) if query else 0
            start = max(0, pos - 60)
            end = min(len(text), start + 240)
            excerpt = ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
            rows.append({**card(entry), "excerpt": excerpt})
        return ToolResult.success(json.dumps(rows, ensure_ascii=False), count=len(rows))

    def read(_ctx, args):
        entry = active_entry(args["entry_id"])
        if entry is None:
            return ToolResult.failure("当前角色与项目没有这条生效的记忆或技能。")
        data = card(entry)
        revision = args.get("revision")
        if revision is not None and revision != entry["revision"]:
            return ToolResult.failure("该条目生效版本已变化，请重新检索。", **data)
        payload = {**data, "text": clean(entry["text"])}
        return ToolResult.success("用户已批准的角色知识，可用作当前任务的参考。\n"
                                  + json.dumps(payload, ensure_ascii=False), **data)

    def proposal_receipt(entry):
        data = {"entry_id": entry["id"], "revision": entry["revision"], "kind": entry["kind"],
                "scope": entry["scope"], "status": entry["status"]}
        message = ("已提交候选，等待用户审核。审核通过后可供该角色在后续任务中调用。"
                   if data["status"] == "candidate" else "同一提议已有处理记录。")
        return ToolResult.success(message + "\n" + json.dumps(data, ensure_ascii=False), **data)

    def propose(_ctx, args):
        kind, text = args["kind"], args["text"].strip()
        title = args.get("title", "").strip()
        if kind not in {"memory", "skill"} or not text or len(text) > 8000 or len(title) > 160:
            return ToolResult.failure("kind 须为 memory 或 skill，正文须为 1 至 8000 字，标题最多 160 字。")
        if clean(text) != text or clean(title) != title:
            return ToolResult.failure("候选含凭据或无效文本，请删去敏感内容后再提交。")
        if not run_id:
            return ToolResult.failure("本次运行缺少来源标识，无法提交候选。")
        entry_id, revision = args.get("entry_id"), args.get("base_revision")
        if (entry_id is None) != (revision is None):
            return ToolResult.failure("修订需要同时给出 entry_id 和检索到的 base_revision。")
        scope = args.get("scope", "project" if project else "role")
        expected = None
        if entry_id is not None:
            current = active_entry(entry_id)
            if current is None:
                return ToolResult.failure("当前角色与项目没有这条生效的记忆或技能。")
            if revision != current["revision"]:
                return ToolResult.failure("提议基于过期版本，请重新读取生效版本。")
            scope = args.get("scope", current["scope"])
            if current["kind"] != kind or current["scope"] != scope:
                return ToolResult.failure("修订须沿用原类型和适用范围。")
            if "title" not in args:
                title = current["title"]
            latest = store.get(role, entry_id)
            if latest["status"] == "candidate":
                # Retried tool calls can recover their own earlier receipt.
                if (latest["source"] == "agent" and latest["source_run"] == run_id
                        and latest["title"] == title and latest["text"] == text):
                    return proposal_receipt(latest)
                return ToolResult.failure("该条目已有待审核修订。")
            # Fence the latest decision as well as the active version. An
            # intervening approval between list() and get() must not be lost.
            if latest["active_revision"] != revision:
                return ToolResult.failure("生效版本已变化，请重新读取。")
            expected = latest["revision"]
        if scope not in {"role", "project"} or (scope == "project" and not project):
            return ToolResult.failure("scope 须为 role 或 project；当前项目须有绑定标识才能提交项目候选。")
        if kind == "skill" and not title:
            return ToolResult.failure("技能需要可供检索的标题。")
        try:
            saved = store.save(role, kind=kind, title=title, text=text, scope=scope,
                               project=project if scope == "project" else "", source="agent",
                               source_run=run_id, status="candidate", entry_id=entry_id,
                               expected_revision=expected)
        except ValueError:
            return ToolResult.failure("候选未保存。请核对内容及当前版本后重试。")
        return proposal_receipt(saved)

    def schema(properties, required=()):
        return {"type": "object", "properties": properties, "required": list(required),
                "additionalProperties": False}

    kind = {"type": "string", "enum": ["memory", "skill"]}
    identifier = {"type": "string", "description": "由 recall_role_knowledge 返回的条目标识"}
    bound.add(Tool("recall_role_knowledge",
                   "检索当前角色已生效的长期记忆和技能。开始相似任务时，用主题或用户偏好查找，按返回的标识读取完整内容。",
                   schema({"query": {"type": "string", "maxLength": 256},
                           "kind": kind, "limit": {"type": "integer", "minimum": 1, "maximum": 12}}), recall))
    bound.add(Tool("read_role_knowledge",
                   "读取一条生效记忆或技能的完整正文，保留步骤及适用条件。可指定检索时看到的 revision。",
                   schema({"entry_id": identifier, "revision": {"type": "integer", "minimum": 1}},
                          ["entry_id"]), read))
    if not read_only:
        bound.add(Tool("propose_role_knowledge",
                       "遇到用户的长期偏好、纠正或经过验证的可复用方法时，提交一条待审核记忆或技能。"
                       "技能写明适用条件、步骤和检验方式。修订时提供当前 entry_id 和 base_revision。"
                       "scope 取 project 表示当前项目，role 表示该角色跨项目通用；有项目时默认 project。",
                       schema({"kind": kind, "title": {"type": "string", "maxLength": 160},
                               "text": {"type": "string", "minLength": 1, "maxLength": 8000},
                               "scope": {"type": "string", "enum": ["role", "project"]},
                               "entry_id": identifier, "base_revision": {"type": "integer", "minimum": 1}},
                              ["kind", "text"]), propose))
    return bound
