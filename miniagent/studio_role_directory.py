"""Public directory projections from saved profiles, room snapshots and actual task assignments."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from .studio_conversations import ROLE_DUTIES, ROLE_LABELS, member_profiles, thread_info


def builtin_profiles():
    return [{"id": role, "name": name, "prompt": ROLE_DUTIES[role]} for role, name in ROLE_LABELS.items()]


def room_role_cards(thread, saved, linked):
    """A local member ID identifies its saved profile only when the host bound the room."""
    if thread.get("superagent"):
        return {}
    bound = set(thread.get("saved_roles", [])) | set(linked)
    cards = {}
    for member, profile in member_profiles(thread).items():
        if member in saved and member in bound:
            card = {**saved[member], "source": "saved", "key": member}
        elif (member in ROLE_LABELS and profile["name"] == ROLE_LABELS[member]
              and profile["prompt"] in ("", ROLE_DUTIES[member])):
            card = {**profile, "prompt": ROLE_DUTIES[member], "source": "builtin", "key": member}
        else:
            # Group exact public briefs, including across independent rooms. The
            # digest is outside the namespace of admitted personal IDs (max 48 chars).
            brief = [member, profile["name"], profile["prompt"]]
            digest = hashlib.sha256(json.dumps(brief, ensure_ascii=False).encode()).hexdigest()
            card = {**profile, "source": "historical", "key": "history-" + digest}
        cards[member] = card
    return cards


def _assigned_tasks(service, path, member, room, meta):
    with service._record_snapshot(path) as records:
        tasks = records[1].list("TaskRecord") if records else []
    kind = room["kind"] if room else "run"
    result = []
    for task in tasks:
        if task.assignee_profile_id != member or task.root_id != path.name:
            continue
        participation = "member"
        if kind == "dm":
            participation = "solo"
        elif not task.parent_id and meta.get("mode") == "team":
            participation = "lead"
        result.append({"run_id": path.name, "task_id": task.task_id, "parent_id": task.parent_id,
                       "conversation_id": room["id"] if room else None, "kind": kind,
                       "conversation_title": room["title"] if room else meta.get("title", ""),
                       "objective": task.objective, "status": task.status,
                       "updated_at": task.updated_at, "participation": participation})
    return result


def role_directory(service, key=None):
    from .studio_service import StudioError

    saved = {p["id"]: p for p in service.profiles()}
    roles = {p["id"]: {**p, "key": p["id"], "source": "saved"} for p in saved.values()}
    roles.update({p["id"]: {**p, "key": p["id"], "source": "builtin"} for p in builtin_profiles()})
    rooms = {room["id"]: room for room in service.directory_chats()["recent"]}
    links = {}
    memberships = defaultdict(set)
    task_sources = []
    incomplete = []
    for path in service._directories():
        try:
            meta = service._meta(path)
            thread = thread_info(path.name, meta)
            if thread is None:
                # Older orchestrator runs have no room roster or chat. Their
                # built-in assignees are still authoritative task identities.
                if key in ROLE_LABELS and meta.get("mode") in {"team", "team-demo"}:
                    task_sources.append((path, key, None, meta))
                continue
            if thread.get("superagent"):
                continue
            room_id = thread["id"]
            if room_id not in rooms:
                continue
            if saved and room_id not in links:
                links[room_id] = service.linked_roles(room_id)
            if path.name == room_id:
                current = service._member_state(thread, saved) if thread["kind"] == "group" else thread
                for card in room_role_cards(current, saved, links.get(room_id, ())).values():
                    roles[card["key"]] = card
                    memberships[card["key"]].add(room_id)
            # Assignments remain attributable to the roster frozen for this
            # turn, including members who have since left the channel.
            for member, card in room_role_cards(thread, saved, links.get(room_id, ())).items():
                role_key = card["key"]
                roles[role_key] = card
                if key == role_key and meta.get("mode") != "room":
                    task_sources.append((path, member, rooms[room_id], meta))
        except (OSError, ValueError, TypeError, KeyError):
            incomplete.append(path.name)

    def ordering(room):
        return room["updated_at"], room["id"]

    for role_key, card in roles.items():
        members = memberships[role_key]
        card["conversation_count"] = len(members)
        card["last_active_at"] = max((rooms[r]["updated_at"] for r in members), default=None)
    if key is None:
        items = sorted(roles.values(), key=lambda row: (row["name"].casefold(), row["key"]))
        return service._redact({"roles": items, "incomplete_run_ids": incomplete})
    if key not in roles:
        raise StudioError("找不到这个角色。", "not_found", 404)

    conversations = sorted((rooms[r] for r in memberships[key]), key=ordering, reverse=True)
    tasks = []
    for path, member, room, meta in task_sources:
        try:
            tasks.extend(_assigned_tasks(service, path, member, room, meta))
        except (OSError, ValueError):
            incomplete.append(path.name)
    tasks.sort(key=lambda row: (row["updated_at"], row["run_id"], row["task_id"]), reverse=True)
    return service._redact({"role": roles[key], "tasks": tasks, "conversations": conversations,
                            "incomplete_run_ids": incomplete})
