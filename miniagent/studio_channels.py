"""The local sidebar references stable conversation IDs independently of their turns."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from .studio_conversations import ROLE_LABELS, _valid_id, member_profiles, thread_info
from .studio_workspace import WorkspaceError, select_root


class StudioChannels:
    def _member_state(self, root, saved=None):
        """Current channel membership, distinct from the immutable roster in each turn."""
        from .studio_service import StudioError, _read_file
        try:
            data, truncated = _read_file(self._directory(root["id"]), "studio-members.json", 512 * 1024)
        except StudioError as exc:
            if exc.code != "not_found":
                raise
            state = {"members": root["members"], "lead": root.get("group_lead", root["recipient"]), "profiles": root.get("profiles", []),
                     "saved_roles": root.get("saved_roles", []), "revision": 0}
        else:
            state = self._decode_roster(data, truncated)
        if state["saved_roles"]:
            if saved is None:
                saved = {p["id"]: p for p in self.profiles()}
            state["profiles"] = [saved.get(p["id"], p) if p["id"] in state["saved_roles"] else p
                                 for p in state["profiles"]]
        return state

    def _decode_roster(self, data, truncated):
        from .studio_service import StudioError
        try:
            state = json.loads(data)
            if (truncated or not isinstance(state, dict)
                    or set(state) != {"members", "lead", "profiles", "saved_roles", "revision"}
                    or type(state["revision"]) is not int or state["revision"] < 1):
                raise ValueError
            self._validate_roster(state["members"], state["lead"])
            members = state["members"]
            if (not isinstance(state["profiles"], list) or not isinstance(state["saved_roles"], list)
                    or not all(isinstance(p, dict) and set(p) == {"id", "name", "prompt"} and p["id"] in members
                               and all(isinstance(p[k], str) and 0 < len(p[k]) <= limit
                                       for k, limit in (("name", 100), ("prompt", 4000)))
                               for p in state["profiles"])
                    or len({p["id"] for p in state["profiles"]}) != len(state["profiles"])
                    or set(members) - ROLE_LABELS.keys() - {p["id"] for p in state["profiles"]}
                    or set(state["saved_roles"]) - {p["id"] for p in state["profiles"]}):
                raise ValueError
        except (ValueError, UnicodeError, TypeError):
            raise StudioError("群成员资料无法读取。", "storage_error", 409) from None
        return state

    @staticmethod
    def _validate_roster(members, lead):
        from .studio_service import StudioError
        if (not isinstance(members, list) or len(members) > 16 or not all(_valid_id(role) for role in members)
                or len({role.casefold() for role in members}) != len(members)):
            raise StudioError("请选择最多 16 位不同的群成员。", "invalid_members")
        if (members and lead not in members) or (not members and lead is not None):
            raise StudioError("有成员的群须从成员中选一位负责人，空群的负责人应为空。", "invalid_group_lead")

    def _membership_root(self, room_id):
        from .studio_service import StudioError
        thread = thread_info(room_id, self._meta(self._directory(room_id)))
        if not thread or thread["kind"] != "group" or thread["id"] != room_id:
            raise StudioError("请选择一个已有群聊。", "invalid_channel")
        return thread

    def _roster_response(self, room_id, state):
        profiles = member_profiles(state)
        return self._redact({"conversation_id": room_id, "revision": state["revision"],
                             "members": state["members"], "lead": state["lead"],
                             "member_names": {role: p["name"] for role, p in profiles.items()}})

    def channel_members(self, room_id):
        return self._roster_response(room_id, self._member_state(self._membership_root(room_id)))

    def update_members(self, room_id, payload):
        from .studio_service import StudioError, _write_json
        if (not isinstance(payload, dict) or set(payload) != {"members", "lead", "revision"}
                or type(payload["revision"]) is not int or payload["revision"] < 0):
            raise StudioError("请提供成员、负责人和正在显示的成员版本。")
        members, lead = payload["members"], payload["lead"]
        self._validate_roster(members, lead)
        with self._lock, self._admission():
            root = self._membership_root(room_id)
            state = self._member_state(root)
            if state["revision"] != payload["revision"]:
                raise StudioError("群成员已被其他窗口修改，请刷新成员名单。", "stale_membership", 409)
            if state["members"] == members and state["lead"] == lead:
                return self._roster_response(room_id, state)
            saved = {p["id"]: p for p in self.profiles()}
            profiles = {p["id"]: p for p in state["profiles"]}
            linked = set(self.linked_roles(room_id))
            bound = set(state["saved_roles"]) | linked
            for role in members:
                if role not in state["members"]:
                    if role in saved:
                        bound.add(role)
                        profiles[role] = saved[role]
                    elif role not in ROLE_LABELS:
                        raise StudioError("找不到可邀请的角色：" + role, "unknown_member")
                elif role in bound and role in saved:
                    profiles[role] = saved[role]
            updated = {"members": members, "lead": lead, "revision": state["revision"] + 1,
                       "saved_roles": [role for role in members if role in bound],
                       "profiles": [profiles[role] for role in members if role in profiles]}
            _write_json(self._directory(room_id), "studio-members.json", updated)
            return self._roster_response(room_id, updated)

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
        if thread and thread["kind"] == "group":
            roster = self._member_state(thread)
            row = {**row, "members": roster["members"]}
            profiles = member_profiles(roster)
        else:
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
                profiles = member_profiles(self._member_state(thread))
                names = [profile["name"] for profile in profiles.values()]
                settings["name"] = (("、".join(names[:2]) + ("等" if len(names) > 2 else "") + "的群聊")[:100]
                                    if names else "群聊")
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
            roster = self._member_state(thread)
            return {"kind": "group", "id": identity, "name": name, "members": roster["members"], "lead": roster["lead"]}

    def _publish_room(self, identity, name, members, lead, local, origin):
        """Publish one native ordinal-zero room while holding the cross-server admission lock."""
        from .studio_service import StudioError, _now, _write_json
        destination = self.runs_dir / identity
        self._validate_roster(members, lead)
        try:
            message = {"kind": "group", "members": members, "recipient": lead, "intent": "edit"}
            if members:
                message = self.validate_role_refs(message)
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
