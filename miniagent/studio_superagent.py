"""Tools and durable entry points for the personal Studio assistant."""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path

from .schemas import ToolResult
from .studio_conversations import ROLE_LABELS
from .tools import Tool

ASSISTANT_INSTRUCTIONS = (
    "你是 Superagent，用户与个人 Agent 工作室的统一对话助手。日常请求可直接回答或使用工作区工具交付文件。"
    "用户要求创建长期角色、组建群聊时，使用工作室工具真正保存，并依据其回执报告结果。"
    "先用 list_studio_roles 查找合适的现有成员；角色职责写入 prompt，群聊选择成员及负责人。"
    "新群创建后等用户进入发消息。仅讨论建议时无需创建；缺少必要的业务信息可直接在对话里问用户。"
)


class StudioAssistant:
    @staticmethod
    def _claim_assistant_turn(directory):
        # The SQLite writer lock lives for this run, including work outside the
        # admission transaction. The OS releases it when a Studio process exits.
        from .studio_service import StudioError
        path = directory / ".assistant-owner.sqlite3"
        if path.is_symlink() or path.with_name(path.name + "-journal").is_symlink():
            raise StudioError("对话执行锁文件无效。", "storage_error", 409)
        conn = sqlite3.connect(path, timeout=0, check_same_thread=False)
        try:
            conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            conn.close()
            raise
        return conn

    def _assistant_elsewhere(self, directory):
        if not (directory / ".assistant-owner.sqlite3").exists():
            return False
        try:
            lease = self._claim_assistant_turn(directory)
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                return True
            raise
        lease.close()
        return False

    def _release_assistant_turn(self, run_id):
        lease = self._assistant_turns.pop(run_id, None)
        if lease is not None:
            lease.close()

    def superagent_head(self):
        from .studio_service import StudioError
        head = None
        ordinal = 0
        with self._lock:
            for directory in self._directories():
                try:
                    thread = self._meta(directory).get("thread", {})
                except StudioError:
                    continue
                if thread.get("superagent") and thread["ordinal"] > ordinal:
                    ordinal = thread["ordinal"]
                    head = (thread["id"], directory.name)
        return {"conversation_id": head[0] if head else None, "head_run_id": head[1] if head else None}

    def send_superagent(self, payload):
        from .studio_service import StudioError
        if (not isinstance(payload, dict) or not {"request_id", "message"} <= payload.keys()
                <= {"request_id", "message", "parent_run_id", "workspace_path"}):
            raise StudioError("请提供消息、请求 ID 和当前对话末轮。")
        parent = payload.get("parent_run_id")
        if parent is not None and (not isinstance(parent, str) or not parent):
            raise StudioError("对话末轮标识无效。")
        turn = {"intent": "edit", "recipient": "worker"}
        if parent is None:
            turn.update(kind="dm", members=["worker"])
        else:
            turn["parent_run_id"] = parent
        request = {"mode": "single", "objective": payload["message"], "request_id": payload["request_id"],
                   "conversation": turn}
        if "workspace_path" in payload:
            request["workspace_path"] = payload["workspace_path"]
        return self.start(request, superagent=True)

    def _create_group(self, run_id, name, members, lead):
        from .studio_service import StudioError, _now, _write_json
        if not name.strip() or len(name.strip()) > 100:
            raise StudioError("群名应为 1 至 100 个字符。")
        name = name.strip()
        # Repeating the same creation call in a turn returns the existing room.
        # Scope the key to the turn so a later explicit request may reuse a title.
        identity = "group-" + uuid.uuid5(uuid.NAMESPACE_URL, run_id + "/" + name).hex
        destination = self.runs_dir / identity
        with self._lock, self._admission():
            message = self.validate_role_refs({"kind": "group", "members": members, "recipient": lead,
                                               "intent": "edit"})
            if destination.exists() or destination.is_symlink():
                old = self._meta(self._directory(identity))
                thread = old.get("thread", {})
                if (old.get("mode") != "room" or old.get("created_by_run_id") != run_id
                        or set(thread.get("members", [])) != set(members) or thread.get("recipient") != lead):
                    raise StudioError("本轮已有同名群聊且配置不同，请使用其他群名。", "group_conflict", 409)
            else:
                thread = {**self.bind_roles(message), "id": identity, "ordinal": 0, "parent_run_id": None,
                          "base_run_id": None, "knowledge_project_id": identity}
                staged = Path(tempfile.mkdtemp(prefix=".studio-pending-", dir=self.runs_dir))
                try:
                    workspace = self._meta(self._directory(run_id)).get("workspace_path")
                    _write_json(staged, "studio.json", {
                        "schema_version": 1, "mode": "room", "state": "ready", "title": self._redact(name),
                        "created_at": _now(), "created_by_run_id": run_id, "thread": thread,
                        "workspace_path": workspace})
                    staged.rename(destination)
                finally:
                    if staged.exists():
                        shutil.rmtree(staged)
            return {"kind": "group", "id": identity, "name": self._redact(name),
                    "members": list(thread["members"]), "lead": lead}

    def add_superagent_tools(self, registry, run_id):
        def record(entity):
            # Replays may recover the entity after a crash between mutation and journaling.
            with self._lock:
                if not any(row.get("entity") == entity for row in self.room_events(run_id)):
                    self._record_room_event(run_id, "superagent", "studio_entity", entity["name"], entity=entity)
            return ToolResult.success(json.dumps(entity, ensure_ascii=False), entity=entity)

        def list_roles(ctx, args):
            rows = [{"id": key, "name": value} for key, value in ROLE_LABELS.items()]
            rows.extend(self.profiles())
            start = args.get("offset", 0)
            if not 0 <= start <= len(rows):
                return ToolResult.failure("offset 超出角色目录范围。")
            return ToolResult.success(json.dumps({"roles": rows[start:start + 24],
                "next_offset": start + 24 if start + 24 < len(rows) else None}, ensure_ascii=False))

        def create_role(ctx, args):
            profile = self.add_profile(args)
            return record({"kind": "role", "id": profile["id"], "name": profile["name"]})

        def create_group(ctx, args):
            return record(self._create_group(run_id, args["name"], args["members"], args["lead"]))

        def register(name, description, fields, required, fn):
            registry.add(Tool(name, description, {"type": "object", "properties": fields,
                "required": required, "additionalProperties": False}, fn))

        register("list_studio_roles", "列出现有角色、名称与自定义职责。用返回的 id 组群或联系角色。",
                 {"offset": {"type": "integer", "minimum": 0}}, [], list_roles)
        register("create_studio_role", "保存可长期私聊和参与群工作的角色。id 为字母开头，支持字母、数字、短划线及下划线。",
                 {"id": {"type": "string", "maxLength": 48}, "name": {"type": "string", "maxLength": 100},
                  "prompt": {"type": "string", "maxLength": 4000}}, ["id", "name", "prompt"], create_role)
        register("create_studio_group", "用已存在的角色 ID 建立持久群聊，选择其中一位为负责人。不会代发第一条任务。",
                 {"name": {"type": "string", "maxLength": 100},
                  "members": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 16},
                  "lead": {"type": "string"}}, ["name", "members", "lead"], create_group)
