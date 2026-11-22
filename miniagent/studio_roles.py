"""Bind stable personal profiles to conversations and expose their curated library."""
from __future__ import annotations

import json
import re

from .role_tools import attach_role_tools
from .studio_conversations import validate_conversation
from .studio_role_directory import role_directory, room_role_cards
from .studio_role_memory import RoleMemoryConflict, RoleMemoryNotFound


def knowledge_project(thread, workspace, run_id, previous=None):
    """Workspace paths are normalized at admission; legacy histories may only carry a reset."""
    if workspace:
        return workspace
    if thread.get("knowledge_project_id"):
        return thread["knowledge_project_id"]
    if thread.get("inherit_files") is False:
        return run_id
    return previous or thread["id"]


class RoleSession:
    """Only the host may choose whose knowledge a model sees."""

    def __init__(self, store, thread, project, run_id, secret, history=()):
        self.store = store
        self.saved = frozenset(thread.get("saved_roles", []))
        self.project = knowledge_project(thread, project, run_id)
        self.run_id = run_id
        self.secret = secret
        self.conversation_id = thread["id"] if thread["kind"] == "dm" else None
        self.default_scope = "conversation" if thread["kind"] == "dm" else "project"
        self.conversation_runs = {run_id, *(turn["run_id"] for turn in history if turn.get("run_id"))}

    def registry(self, role, registry, *, read_only=False):
        if role not in self.saved:
            return registry
        return attach_role_tools(registry, self.store, role, self.project, self.run_id,
                                 read_only=read_only, secret=self.secret, conversation_runs=self.conversation_runs,
                                 conversation_id=self.conversation_id, default_scope=self.default_scope)

    def context(self, role, objective):
        if role not in self.saved:
            return ""
        return self.store.context(role, self.project, objective, conversation_runs=self.conversation_runs,
                                  conversation_id=self.conversation_id)

    def remember_instruction(self, thread, text):
        role = thread["recipient"]
        if thread["kind"] != "dm" or role not in self.saved:
            return ""
        match = re.fullmatch(r"(?:/remember\s+|(?:请)?记住\s*[：:]\s*|remember that\s+)(.+)",
                             text.strip(), re.IGNORECASE | re.DOTALL)
        if not match:
            return ""
        try:
            self.store.save(role, kind="memory", text=match[1].strip(), scope="conversation",
                            project=self.conversation_id, source_run=self.run_id)
        except ValueError as exc:
            return "宿主未能保存本轮要求的长期记忆：" + str(exc)
        return "已保存为本次私聊的记忆，后续在这次私聊中可继续使用。"


class StudioRoles:
    def _note_conversation(self, run_id):
        from .studio_service import StudioError
        try:
            thread = self._meta(self._directory(run_id)).get("thread") or {}
            return thread.get("id") if thread.get("kind") == "dm" else None
        except StudioError:
            # Unresolvable provenance remains private and can be retried on restart.
            return None

    def model_role_briefs(self, *, room_id=None, source_run=None):
        public = []
        for row in self._profile_records():
            authored = row.get("source_run")
            visible = (not authored or authored == source_run
                       or (room_id is not None and room_id == row.get("brief_room")))
            public.append({"id": row["id"], "name": row["name"],
                           "prompt": row["prompt"] if visible else row["name"]})
        return public

    def room_role_briefs(self, room_id, profiles, bound, *, source_run=None):
        if not bound:
            return profiles
        scoped = {row["id"]: row for row in self.model_role_briefs(room_id=room_id, source_run=source_run)}
        return [scoped[p["id"]] if p["id"] in bound and p["id"] in scoped else p for p in profiles]

    def _claim_role_briefs(self, source_run, room_id, members):
        """The first room created for the original assignment gets the draft brief.
        The caller holds the admission lock; repeating the claim is idempotent.
        """
        from .studio_service import _write_json
        rows = self._profile_records()
        claimed = [row for row in rows if row["id"] in members
                   and row.get("source_run") == source_run and not row.get("brief_room")]
        if claimed:
            for row in claimed:
                row["brief_room"] = room_id
            _write_json(self.runs_dir, "studio-profiles.json", rows)

    def _upgrade_role_briefs(self):
        """Recover old automatic briefs from successful creation receipts and exact tool arguments."""
        from .studio_service import StudioError, _write_json
        if all("source_run" in row for row in self._profile_records()):
            return
        with self._lock, self._admission():
            rows = self._profile_records()
            legacy = {p["id"]: p for p in rows if "source_run" not in p}
            if not legacy:
                return
            for path in reversed(self._directories()):
                try:
                    if not self._meta(path).get("thread", {}).get("superagent"):
                        continue
                    entities = [e["entity"] for e in self.room_events(path.name) if e.get("kind") == "studio_entity"]
                    created = {e["id"] for e in entities if e["kind"] == "role"} & legacy.keys()
                    if not created:
                        continue
                    with self._record_snapshot(path) as snapshot:
                        if not snapshot:
                            continue
                        calls = snapshot[0].execute("SELECT payload FROM blobs WHERE kind='action_identity'")
                        for data, in calls:
                            action = json.loads(data)
                            brief = action.get("arguments") or {}
                            if action.get("operation") != "create_studio_role" or brief.get("id") not in created:
                                continue
                            if legacy.get(brief["id"]) != brief:
                                continue
                            row = legacy.pop(brief["id"])
                            row["source_run"] = path.name
                            for entity in entities:
                                if entity["kind"] == "group" and row["id"] in entity.get("members", []):
                                    row["brief_room"] = entity["id"]
                                    break
                except (StudioError, KeyError, ValueError):
                    continue
            for row in legacy.values():
                row["source_run"] = None
            _write_json(self.runs_dir, "studio-profiles.json", rows)

    def role_directory(self, key=None):
        return role_directory(self, key)

    def directory_role_keys(self, thread, linked, *, saved=None):
        if thread.get("superagent"):
            return {}
        if saved is None:
            saved = {p["id"]: p for p in self.profiles()}
        return {member: card["key"] for member, card in room_role_cards(thread, saved, linked).items()}

    def validate_role_refs(self, value):
        if not isinstance(value, dict) or "parent_run_id" in value:
            return validate_conversation(value)
        supplied, members = value.get("profiles", []), value.get("members")
        if not isinstance(supplied, list) or not isinstance(members, list):
            return validate_conversation(value)
        ids = [row.get("id") for row in supplied if isinstance(row, dict)]
        implicit = [p for p in self.profiles() if p["id"] in members and p["id"] not in ids]
        checked = validate_conversation({**value, "profiles": [*supplied, *implicit]})
        # A saved profile is referenced by identity. Keep its mutable settings
        # out of request fingerprints so a retry after editing remains a replay.
        if supplied:
            checked["profiles"] = supplied
        else:
            checked.pop("profiles", None)
        return checked

    def bind_roles(self, message):
        from .studio_service import StudioError
        saved = {p["id"]: p for p in self.profiles()}
        custom = {p["id"]: p for p in message.get("profiles", [])}
        bound = [role for role in message["members"] if role in saved]
        for role in bound:
            if role in custom and custom[role] != saved[role]:
                raise StudioError("已保存角色的资料已变化。请刷新角色资料后重试。", "profile_conflict", 409)
            custom[role] = saved[role]
        result = {**message, "saved_roles": bound}
        if custom:
            result["profiles"] = list(custom.values())
        return result

    def current_role(self, role):
        from .studio_service import StudioError
        profile = next((p for p in self.profiles() if p["id"] == role), None)
        if profile is None:
            raise StudioError("找不到这个已保存的角色。", "not_found", 404)
        return profile

    def edit_profile(self, role, payload):
        from .studio_service import StudioError, _write_json
        if (not isinstance(payload, dict) or not {"name", "prompt", "previous"} <= payload.keys()
                <= {"name", "prompt", "previous", "share_brief"} or type(payload.get("share_brief", False)) is not bool):
            raise StudioError("请提供角色名称和职责。")
        profile = {"id": role, "name": payload["name"], "prompt": payload["prompt"]}
        try:
            validate_conversation({"kind": "dm", "members": [role], "recipient": role,
                                   "intent": "question", "profiles": [profile]})
        except ValueError as exc:
            raise StudioError(str(exc)) from None
        with self._lock, self._admission():
            rows = self._profile_records()
            old = next((p for p in rows if p["id"] == role), None)
            if old is None:
                raise StudioError("找不到这个已保存的角色。", "not_found", 404)
            prior = {k: old[k] for k in ("name", "prompt")}
            scoped_prior = {**prior, "brief_scope": "source" if old.get("source_run") else "role"}
            if payload["previous"] not in (prior, scoped_prior):
                raise StudioError("其他窗口已修改角色。请刷新后重试。", "profile_conflict", 409)
            profile = self._redact(profile)
            old.update(profile)
            if payload.get("share_brief"):
                old["source_run"] = None
                old.pop("brief_room", None)
            if len(json.dumps(rows, ensure_ascii=False).encode()) > 2 * 1024**2:
                raise StudioError("角色资料超过存储上限。", "profile_storage", 409)
            _write_json(self.runs_dir, "studio-profiles.json", rows)
            return profile

    def linked_roles(self, room_id):
        from .studio_service import _read_file
        data, _ = _read_file(self._directory(room_id), "studio-linked-roles.json", 8192, missing=True)
        return json.loads(data) if data else []

    def _can_link(self, root, profile):
        # Both old and current unbound snapshots can be adopted. Reading the
        # directory never grants access to personal knowledge; binding is explicit.
        return (profile["id"] not in root.get("saved_roles", []) and profile in root.get("profiles", [])
                and profile["id"] in root.get("members", []))

    def link_profile(self, role, payload):
        from .studio_service import StudioError, _write_json
        if (not isinstance(payload, dict) or set(payload) != {"room_id", "expected_head"}
                or not all(isinstance(value, str) for value in payload.values())):
            raise StudioError("请选择会话及其当前版本。")
        room_id = payload["room_id"]
        with self._lock, self._admission():
            profile = self.current_role(role)
            path = self._directory(room_id)
            root = self._meta(path).get("thread", {})
            current = self._member_state(root) if root.get("id") == room_id and root.get("kind") == "group" else root
            if root.get("id") != room_id or not self._can_link(current, profile):
                raise StudioError("旧会话中没有与本角色资料一致的成员。", "profile_conflict", 409)
            room = self._conversation(path, with_answers=False)
            if room["head_run_id"] != payload["expected_head"] or not room["can_continue"]:
                raise StudioError("请等待当前轮结束，并刷新会话后重试。", "stale_head", 409)
            linked = self.linked_roles(room_id)
            if role not in linked:
                _write_json(path, "studio-linked-roles.json", [*linked, role])
            return {"conversation": {"id": room_id, "head_run_id": room["head_run_id"]}}

    def role_library(self, role):
        profile = self.current_role(role)
        rooms, unbound = [], []
        for row in self.list_conversations()["conversations"]:
            if role not in row["members"]:
                continue
            meta = self._meta(self._directory(row["head_run_id"]))
            thread = meta.get("thread", {})
            root = self._meta(self._directory(row["id"])).get("thread", {})
            roster = self._member_state(root) if root.get("kind") == "group" else thread
            project = meta.get("workspace_path") or thread.get("knowledge_project_id")
            if project is None:
                project = self._conversation(self._directory(row["head_run_id"]), with_answers=False)["knowledge_project_id"]
            item = {**row, "project": project, "status": meta.get("state")}
            if role in roster.get("saved_roles", []) or role in self.linked_roles(row["id"]):
                rooms.append(item)
            elif self._can_link(roster, profile):
                unbound.append(item)
        record = next(p for p in self._profile_records() if p["id"] == role)
        return {"role": profile, "brief_scope": "source" if record.get("source_run") else "role",
                "entries": self.role_memory.list(role, include_inactive=True),
                "active_entries": self.role_memory.list(role), "conversations": rooms,
                "unbound_conversations": unbound}

    def role_operation(self, role, op, payload, entry_id=None):
        from .studio_service import StudioError
        self.current_role(role)
        try:
            if not isinstance(payload, dict):
                raise ValueError("需要 JSON 对象。")
            if op == "history" and not payload:
                return {"history": self.role_memory.history(role, entry_id)}
            if op == "save" and {"kind", "text"} <= payload.keys() <= {
                    "kind", "text", "title", "scope", "project", "entry_id", "expected_revision"}:
                if payload.get("scope") == "conversation" and not payload.get("entry_id"):
                    room = self._conversation(self._directory(payload.get("project")), with_answers=False)
                    if (not room or room["kind"] != "dm" or room["id"] != payload.get("project")
                            or role not in room["saved_roles"]):
                        raise ValueError("请选择此角色的私聊。")
                row = self.role_memory.save(role, **payload)
            elif op == "status" and payload.keys() == {"status", "expected_revision"}:
                row = self.role_memory.set_status(role, entry_id, **payload)
            elif op == "restore" and payload.keys() == {"revision", "expected_revision"}:
                row = self.role_memory.restore(role, entry_id, **payload)
            else:
                raise ValueError("角色知识操作参数无效。")
            return {"entry": row}
        except RoleMemoryConflict as exc:
            raise StudioError(str(exc), "knowledge_conflict", 409) from None
        except RoleMemoryNotFound as exc:
            raise StudioError(str(exc), "not_found", 404) from None
        except (ValueError, TypeError):
            raise StudioError("请检查类型、内容、范围、凭据及版本字段。") from None
