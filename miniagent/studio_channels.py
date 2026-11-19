"""The local sidebar references stable conversation IDs independently of their turns."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from .studio_conversations import member_profiles, thread_info
from .studio_workspace import WorkspaceError, select_root


class StudioChannels:
    def _channel_name(self, value):
        from .studio_service import StudioError
        if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 100
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in value.strip())):
            raise StudioError("群名须为 1 至 100 个单行文本字符。")
        return self._redact(value.strip())

    def _channel_settings(self, room_id):
        from .studio_service import StudioError, _read_file
        raw, truncated = _read_file(self._directory(room_id), "studio-channel.json", 2048, missing=True)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
            if (truncated or not isinstance(value, dict) or type(value.get("pinned")) is not bool
                    or set(value) - {"pinned", "name"}
                    or "name" in value and not isinstance(value["name"], str)):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise StudioError("固定群聊配置无法读取。", "storage_error", 409) from None
        return value

    def _display_settings(self, room_id):
        from .studio_service import StudioError
        # A damaged sidebar preference must not hide the conversation or block a turn.
        try:
            return self._channel_settings(room_id)
        except StudioError:
            return {}

    def channel_label(self, room_id, root_meta):
        return self._display_settings(room_id).get("name") or (
            root_meta.get("title") if root_meta.get("mode") == "room" else None)

    def _room_card(self, row, root):
        thread = thread_info(row["id"], root)
        profiles = member_profiles(thread) if thread else {}
        names = [profiles.get(role, {}).get("name", role) for role in row["members"]]
        anchor = root.get("mode") == "room"
        settings = self._display_settings(row["id"]) if row["kind"] == "group" and thread else {}
        return {**row, "superagent": bool(row.get("superagent")), "member_names": names,
                "title": settings.get("name") or row["title"], "pinned": settings.get("pinned", anchor)}

    def directory_chats(self):
        rows = self._conversation_rows(directory=True)
        return {"recent": rows, "channels": sorted((row for row in rows if row["pinned"]),
                                                  key=lambda row: (row["title"].casefold(), row["id"]))}

    def _channel_row(self, room_id):
        return next(row for row in self.directory_chats()["recent"] if row["id"] == room_id)

    def update_channel(self, room_id, payload):
        from .studio_service import StudioError, _write_json
        if (not isinstance(payload, dict) or set(payload) - {"pinned", "name"}
                or type(payload.get("pinned")) is not bool):
            raise StudioError("请提供固定状态及可选的群名。")
        name = self._channel_name(payload["name"]) if "name" in payload else None
        with self._lock, self._admission():
            path = self._directory(room_id)
            meta = self._meta(path)
            thread = thread_info(room_id, meta)
            if not thread or thread["id"] != room_id or thread["kind"] != "group":
                raise StudioError("请选择一个已有群聊。", "invalid_channel")
            settings = self._channel_settings(room_id)
            if name:
                settings["name"] = name
            elif payload["pinned"] and "name" not in settings and meta.get("mode") != "room":
                profiles = member_profiles(thread)
                names = [profile["name"] for profile in profiles.values()]
                settings["name"] = ("、".join(names[:2]) + ("等" if len(names) > 2 else "") + "的群聊")[:100]
            settings["pinned"] = payload["pinned"]
            _write_json(path, "studio-channel.json", settings)
            return self._channel_row(room_id)

    def create_channel(self, payload):
        from .studio_service import StudioError
        if (not isinstance(payload, dict) or not {"request_id", "name", "members", "lead"} <= payload.keys()
                <= {"request_id", "name", "members", "lead", "workspace_path"}):
            raise StudioError("请提供群名、成员、负责人和请求 ID。")
        try:
            request_id = str(uuid.UUID(payload["request_id"]))
        except (AttributeError, TypeError, ValueError):
            raise StudioError("request_id 必须是 UUID。") from None
        try:
            local = select_root(payload["workspace_path"], self.runs_dir) if "workspace_path" in payload else None
        except WorkspaceError as exc:
            raise StudioError(str(exc), "workspace_unavailable", 409) from None
        identity = "group-" + uuid.uuid5(uuid.NAMESPACE_URL, "studio-channel/" + request_id).hex
        with self._lock, self._admission():
            self._publish_room(identity, self._channel_name(payload["name"]), payload["members"], payload["lead"],
                               local, {"channel_request_id": request_id})
            return self._channel_row(identity)

    def _create_group(self, run_id, name, members, lead):
        # The same assistant tool call in a turn must return the same room on replay.
        name = self._channel_name(name)
        identity = "group-" + uuid.uuid5(uuid.NAMESPACE_URL, run_id + "/" + name).hex
        with self._lock, self._admission():
            local = self._meta(self._directory(run_id)).get("workspace_path")
            thread = self._publish_room(identity, name, members, lead, local, {"created_by_run_id": run_id})
            return {"kind": "group", "id": identity, "name": name, "members": thread["members"], "lead": lead}

    def _publish_room(self, identity, name, members, lead, local, origin):
        """Publish one native ordinal-zero room while holding the cross-server admission lock."""
        from .studio_service import StudioError, _now, _write_json
        destination = self.runs_dir / identity
        try:
            message = self.validate_role_refs({"kind": "group", "members": members, "recipient": lead, "intent": "edit"})
        except StudioError:
            raise
        except ValueError as exc:
            raise StudioError(str(exc)) from None
        if destination.exists() or destination.is_symlink():
            old = self._meta(self._directory(identity))
            thread = old.get("thread", {})
            if (old.get("mode") != "room" or any(old.get(key) != value for key, value in origin.items())
                    or old.get("title") != name or old.get("workspace_path") != local
                    or set(thread.get("members", [])) != set(members) or thread.get("recipient") != lead):
                raise StudioError("本次请求已建群且配置不同，请重新提交。", "group_conflict", 409)
            return thread
        thread = {**self.bind_roles(message), "id": identity, "ordinal": 0, "parent_run_id": None,
                  "base_run_id": None, "knowledge_project_id": identity}
        with TemporaryDirectory(prefix=".studio-pending-", dir=self.runs_dir) as stage:
            staged = Path(stage)
            _write_json(staged, "studio.json", {
                "schema_version": 1, "mode": "room", "state": "ready", "title": name, **origin,
                "created_at": _now(), "thread": thread, "workspace_path": local})
            staged.rename(destination)
        return thread
