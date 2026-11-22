"""Read a group's conversation and task receipts without entering an executor."""
from __future__ import annotations

import re

from .studio_conversations import member_profiles, thread_info
from .studio_group_status import _review_snapshot
from .studio_workspace import _project_path


def _clip(value, limit):
    text = value if isinstance(value, str) else ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _terms(text):
    """Names, path components and Han bigrams for retrieving an older topic."""
    words = set(re.findall(r"[a-z0-9_]+", text.casefold()))
    for phrase in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(phrase[i:i + 2] for i in range(max(1, len(phrase) - 1)))
    return words


def _files(names, limit=24):
    return [_clip(name, 240) for name in (names or [])[:limit] if isinstance(name, str)]


def _delivery_receipts(service, path, thread, meta, state, events):
    from .studio_service import _EXTENSIONS
    publication = meta.get("workspace_delivery") or {}
    final = state in {"completed", "unverified"} and publication.get("status") == "published"
    handoffs, refs, shared = [], {}, {}
    for event in events:
        kind, role, task_id = event["kind"], event.get("role"), event.get("task_id")
        if kind not in {"direct_delivery", "member_report"} or event.get("published") is not True:
            continue
        handoff = event.get("handoff") or {}
        if not task_id or not role or handoff.get("role") != role or handoff.get("task_id") != task_id:
            continue
        if kind == "direct_delivery" and (task_id != path.name or role != thread.get("recipient")):
            continue
        changes = {c["path"]: c for c in handoff.get("changed_files", []) if _project_path(c.get("path"), _EXTENSIONS)}
        projected = event.get("workspace_delivery") or {}
        eligible = (set(projected.get("files", [])) if projected.get("status") == "published" else set())
        artifacts = {a.get("project_path"): a for a in event.get("artifacts", [])}
        for name, change in changes.items():
            refs.pop(name, None)  # An edit invalidates the earlier version, even if its new receipt is missing.
            if change.get("change") == "deleted":
                shared.pop(name, None)
                continue
            if change.get("change") not in {"added", "modified"}:
                continue
            shared[name] = True
            if meta.get("workspace_path") and not final and name not in eligible:
                continue
            artifact = artifacts.get(name, {})
            digest = artifact.get("sha256")
            ref = artifact.get("path", "")
            if (isinstance(digest, str) and re.fullmatch("[0-9a-f]{64}", digest)
                    and digest == change.get("sha256") and isinstance(ref, str)
                    and re.fullmatch("handoffs/[0-9a-f]{32}/" + re.escape(name), ref)):
                refs[name] = {"path": name, "ref": ref, "sha256": digest, "role": role, "task_id": task_id}
        handoffs.append({"role": role, "task_id": task_id,
                         "changed_files": [{"path": _clip(name, 240), "change": row.get("change")}
                                           for name, row in list(changes.items())[:12]],
                         "summary": _clip(handoff.get("summary"), 650)})
    if final:
        published = [n for n in publication.get("files", []) if _project_path(n, _EXTENSIONS)]
        archived = {}
        if (path / "workspace-projection.json").exists():
            initial = service._projection_basis(path, initial=True)
            final_basis = service._projection_basis(path)
            if initial["directory"] != final_basis["directory"]:
                published = []
            else:
                published = [n for n in published if n in final_basis["files"]
                             and initial["files"].get(n) != final_basis["files"][n]]
            refs = {n: ref for n, ref in refs.items() if final_basis["files"].get(n) == ref["sha256"]}
            archived = {n: {"path": n, "ref": f"agents/{thread['recipient']}/{n}", "source": "final_workspace",
                            "sha256": final_basis["files"][n]} for n in published}
        # Without a final digest, a historical OS-folder receipt is descriptive only.
        verified = {**archived, **refs}
        refs = {n: verified[n] for n in published if n in verified}
    elif publication or meta.get("workspace_path"):
        published = []
    elif state in {"completed", "unverified"}:
        required = meta.get("request", {}).get("acceptance_files", []) if state == "completed" else []
        published = list(dict.fromkeys([*shared, *(n for n in required if _project_path(n, _EXTENSIONS))]))
    else:
        published = []
    return published, list(refs.values()), handoffs


def published_artifact_catalog(service, path, thread, meta, state, events):
    """UI links refer to the run that published the bytes, even when later turns inherit them."""
    _, refs, _ = _delivery_receipts(service, path, thread, meta, state, events)
    return [{"run_id": path.name, "path": row["path"], "ref": row["ref"]} for row in refs]


def _root_outcome(service, path, thread, state, required, published, drafts):
    from .studio_service import StudioError, _EXTENSIONS, _read_file
    verdict, criteria = None, []
    if (path / "tasks.sqlite3").exists():
        tasks, reports = service._records(path)
        root = next((r for r in tasks if r["task_id"] == path.name and r["parent_id"] is None), {})
        report = next((r["report"] for r in reports if r["task_id"] == path.name
                       and r["report"]["report_id"] == root.get("latest_report_id")), None)
        if report:
            verdict = report["verdict"]
            criteria = [{"criterion_id": c["criterion_id"], "verdict": c["verdict"]} for c in report["checks"][:16]]
    frozen = {name for row in drafts for name in row["files"]}
    public, private, missing = [], [], []
    for name in required:
        if not _project_path(name, _EXTENSIONS):
            continue
        if name in published:
            public.append(name)
            continue
        try:
            present, _ = _read_file(path, f"agents/{thread['recipient']}/{name}", 1, missing=True)
        except StudioError as error:
            if error.code != "invalid_path":
                raise
            present = b""
        if present:
            public.append(name)
        elif name in frozen:
            private.append(name)
        else:
            missing.append(name)
    return {"state": state, "acceptance_verdict": verdict, "acceptance_criteria": criteria,
            "required_present_in_public_project": _files(public), "required_only_in_frozen_draft": _files(private),
            "required_missing_from_public_and_frozen_draft": _files(missing)}


def _task_context(service, path, thread, meta, state, text):
    from .studio_service import _read_file
    events = service.room_events(path.name)
    drafts, reviews = _review_snapshot(thread, events)
    publication = meta.get("workspace_delivery") or {}
    published, refs, handoffs = _delivery_receipts(service, path, thread, meta, state, events)
    required = meta.get("request", {}).get("acceptance_files", [])
    if len(refs) > 32:
        query = text.casefold()
        refs.sort(key=lambda r: (r["path"].casefold() in query, r["path"].rsplit("/", 1)[-1].casefold() in query),
                  reverse=True)
    task = {"run_id": path.name, "ordinal": thread["ordinal"], "objective": _clip(meta.get("title"), 460),
            "state": state, "recipient": thread.get("recipient"), "requested_files": _files(required),
            "workspace_publication": publication.get("status"),
            "published_files": _files(published), "published_file_count": len(published),
            "published_artifacts": refs[:32], "member_handoffs": handoffs[-6:],
            "root_outcome": _root_outcome(service, path, thread, state, required, published, drafts),
            "frozen_candidates": [{**row, "files": _files(row["files"], 12)} for row in drafts[-3:]],
            "reviews": reviews[-5:]}
    if state in {"completed", "unverified"} or thread.get("intent") == "question":
        raw, _ = _read_file(path, "conversation-answer.txt", 5200, missing=True)
        task["answer"] = _clip(raw.decode("utf-8", errors="ignore"), 1600)
    if state in {"running", "queued", "waiting_user"}:
        progress = {}
        for event in events:
            if event["kind"] in {"assistant_progress", "author_probe_pending", "human_question"}:
                progress[event["role"]] = {"role": event["role"], "kind": event["kind"], "text": _clip(event["text"], 300)}
        task["last_observations"] = list(progress.values())[-4:]
    messages = [e for e in events if e["kind"] in {"group_message", "status_reply", "user_steer",
                                                    "human_question", "human_answer"}]
    return task, messages


def _message_view(events, text):
    unique = {}
    for row in events:
        # The chat ledger and old public journals may expose the same exchange.
        key = (row.get("request_id") or row.get("at"), row.get("kind"))
        unique[key] = row
    rows = sorted(unique.values(), key=lambda row: (row.get("at") or "", row.get("seq") or 0))
    selected = set(range(max(0, len(rows) - 14), len(rows)))
    terms = _terms(text)
    older = range(max(0, len(rows) - 14))
    matches = sorted(older, key=lambda i: (len(terms & _terms(rows[i].get("question", "") + " " +
                                                               _clip(rows[i].get("text"), 1200))), i), reverse=True)
    if terms:
        selected.update(matches[:2])
    return [{"role": row.get("role"), "kind": row.get("kind"), "question": _clip(row.get("question"), 380),
             "text": _clip(row.get("text"), 650), "disposition": row.get("disposition"),
             "linked_run_id": row.get("linked_run_id"), "queue_state": row.get("queue_state"),
             "attachments": [_clip(item.get("name"), 140) for item in row.get("message_attachments", [])[:8]]}
            for i, row in enumerate(rows) if i in selected]


def room_context(service, room, *, text, recipient=None):
    """Build a bounded, room-owned view. A failed or cancelled turn never replaces a delivery."""
    from .studio_service import StudioError
    if room.get("kind") != "group":
        raise StudioError("只可读取本群会话。", "invalid_group")
    group_id = room["id"]
    entries = []
    messages = list(room.get("messages", []))
    query = _terms(text)
    for turn in room.get("turns", []):
        path = service._directory(turn["run_id"])
        meta = service._meta(path)
        thread = thread_info(path.name, meta)
        if not thread or thread["id"] != group_id or thread["kind"] != "group":
            continue
        published = (meta.get("workspace_delivery") or {}).get("files") or []
        searchable = " ".join([meta.get("title", ""), *_files(published, 1000),
                               *_files(meta.get("request", {}).get("acceptance_files", []), 32)])
        score = len(query & _terms(searchable))
        if path.name in text or re.search(rf"(?:task\s*#?\s*{thread['ordinal']}\b|第\s*{thread['ordinal']}\s*(?:个?任务|轮))", text, re.I):
            score += len(query) + 1
        entries.append((path, thread, meta, turn["status"], score))
    # Reserve places for the last genuine delivery and the active task even when
    # a more recent run contains hundreds of progress messages or a cancelled draft.
    chosen = set(range(max(0, len(entries) - 2), len(entries)))
    latest_completed = latest_delivery = active = None
    for i, (path, thread, meta, state, _) in enumerate(entries):
        if state == "completed" and thread.get("intent") == "edit":
            latest_completed = i
        if ((meta.get("workspace_delivery") or {}).get("status") == "published"
                or (state == "completed" and meta.get("request", {}).get("acceptance_files"))):
            latest_delivery = i
        if state in {"queued", "running", "waiting_user"}:
            active = i
    chosen.update(i for i in (latest_completed, latest_delivery, active) if i is not None)
    matched = sorted((i for i, entry in enumerate(entries) if entry[-1]),
                     key=lambda i: (entries[i][-1], i), reverse=True)
    chosen.update(matched[:2])
    linked = {row.get("linked_run_id") for row in messages[-2:]}
    chosen.update(i for i, entry in enumerate(entries) if entry[0].name in linked)
    if entries:
        chosen.add(0)  # A reference to the original request should still resolve after many tasks.
    tasks = []
    anchor = service._directory(group_id)
    if service._meta(anchor).get("mode") == "room":
        messages.extend(e for e in service.room_events(group_id) if e["kind"] in {"status_reply", "group_message"})
    for i in sorted(chosen):
        path, thread, meta, state, _ = entries[i]
        task, journal = _task_context(service, path, thread, meta, state, text)
        tasks.append(task)
        messages.extend(journal)
    latest_public = next((t["run_id"] for t in reversed(tasks) if t["published_files"]
                          and t["state"] in {"completed", "unverified"}), None)
    members = [{"id": p["id"], "name": _clip(p["name"], 100),
                "responsibility": _clip(p.get("prompt"), 260)} for p in member_profiles(room).values()]
    return service._redact({"room_id": group_id, "title": _clip(room.get("title"), 160),
                            "members": members, "responding_role": recipient or room.get("lead") or room["recipient"],
                            "task_count": sum(thread.get("intent") == "edit" for _, thread, _, _, _ in entries),
                            "latest_completed_task_id": entries[latest_completed][0].name if latest_completed is not None else None,
                            "latest_published_task_id": latest_public,
                            "active_task_id": entries[active][0].name if active is not None else None,
                            "tasks": tasks, "recent_messages": _message_view(messages, text)})
