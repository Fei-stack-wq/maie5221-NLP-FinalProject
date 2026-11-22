"""Answer progress questions from a group's durable receipts without starting a run."""
from __future__ import annotations

import re

from .studio_conversations import thread_info


_QUESTION = re.compile(
    r"(?:(?:所以|那么?|请问(?:一下)?|我想问(?:一下)?|现在|目前|刚才|之前|上一轮|这次|这个|那个|的|任务|项目|进度|开发|已经|都|到底)*"
    r"(?:完成了?[吗么没]|完成没有|做完了?[吗没]|做完没有|好了吗|做得怎么样了?|怎么样了?|到哪(?:一步|儿)了|"
    r"什么(?:进展|进度|状态)|进展如何|如何了?|还有哪些(?:工作|没完成)|还差什么|验收通过了吗|发布了吗|交付了吗)"
    r"|(?:what(?:'s| is) (?:the |its )?(?:status|progress)|(?:is (?:it|this|the (?:task|project))|are we) "
    r"(?:done|finished|complete|ready)|(?:any |status )update(?:s)?|how(?:'s| is) it going))", re.I)


def _legacy_status_question(text: str) -> bool:
    if not isinstance(text, str):
        return False
    question = text.strip().strip(" \t\n\r?？。.!！")
    question = re.sub(r"(?<=[\u3400-\u9fff])[，,\s]+(?=[\u3400-\u9fff])", "", question)
    return bool(_QUESTION.fullmatch(question))


def is_status_only_turn(meta: dict, events: list[dict]) -> bool:
    """A legacy status question may have been misclassified as an edit."""
    request = meta.get("request", {})
    if (not _legacy_status_question(request.get("objective") or meta.get("title")) or
            any(request.get(k) for k in ("attachments", "acceptance_files", "acceptance_checks", "child_checks",
                                         "required_source_review_paths")) or
            meta.get("workspace_delivery") or meta.get("workspace_member_delivery")):
        return False
    return not any(row.get("kind") in {"assignment", "member_report", "user_steer", "human_answer", "human_review"}
                   for row in events)


_VERDICTS = {"passed": "通过", "changes_requested": "要求修改", "inconclusive": "待补证"}


def _review_snapshot(thread, events):
    candidates, reviews, assigned = {}, {}, {}
    source, recovered = thread.get("recovery_source") or {}, thread.get("recovery_review") or {}
    version = recovered.get("candidate_version")
    task, reviewer = source.get("task_id"), recovered.get("reviewer")
    if (task and recovered.get("source_task") == task and reviewer and reviewer != source.get("role")
            and recovered.get("verdict") in _VERDICTS and re.fullmatch(r"[a-f0-9]{64}", str(version))):
        candidates[task] = {"task_id": task, "role": source["role"], "version": version, "files": []}
        reviews[task, version] = {"role": reviewer, "target": task, "version": version,
                                  "verdict": recovered["verdict"], "criteria": len(recovered.get("findings", []))}

    for event in events:
        role, task = event.get("role"), event.get("task_id")
        if event.get("kind") == "assignment":
            assigned[task] = event.get("recipient")
        if event.get("kind") != "member_report" or assigned.get(task) != role or not task:
            continue
        handoff = event.get("handoff") or {}
        if handoff.get("task_id") != task or handoff.get("role") != role:
            continue
        candidate = handoff.get("reviewable_candidate") or {}
        version = candidate.get("candidate_version")
        if candidate.get("task_id") == task and isinstance(version, str) and re.fullmatch(r"[a-f0-9]{64}", version):
            candidates[task] = {"task_id": task, "role": role, "version": version,
                                "files": handoff.get("deliverable_paths", [])}
        review = handoff.get("native_review") or {}
        signed = review.get("assessment") or {}
        target = review.get("target")
        if (not target or target != signed.get("target") or role != review.get("reviewer")
                or role != signed.get("evaluator") or not signed.get("attempt_id")
                or role == review.get("author") or signed.get("verdict") not in _VERDICTS or not signed.get("results")
                or not re.fullmatch(r"[a-f0-9]{64}", str(review.get("candidate_version", "")))
                or signed.get("candidate_version") != review.get("candidate_version")):
            continue
        candidate = candidates.get(target)
        if candidate and (candidate["version"] != signed["candidate_version"] or candidate["role"] != review.get("author")):
            continue
        if candidate and isinstance(review.get("files"), dict):
            candidate["files"] = [name for name in candidate["files"] if name in review["files"]]
        reviews[(target, signed["candidate_version"])] = {
            "role": role, "target": target, "version": signed["candidate_version"],
            "verdict": signed["verdict"], "criteria": len(signed.get("results", []))}
    return list(candidates.values()), list(reviews.values())


def group_status(service, room, *, source_run_id=None):
    """A routed reader can inspect only its own bound child."""
    for turn in reversed(room["turns"]):
        if source_run_id and turn["run_id"] != source_run_id:
            continue
        path = service._directory(turn["run_id"])
        meta = service._meta(path)
        thread = thread_info(path.name, meta)
        if not thread or thread["id"] != room["id"] or thread["kind"] != "group":
            continue
        events = service.room_events(path.name)
        if source_run_id is None and (thread.get("intent") == "question" or is_status_only_turn(meta, events)):
            continue
        summary = service._summary(path)
        candidates, reviews = _review_snapshot(thread, events)
        required = meta.get("request", {}).get("acceptance_files", [])
        # Inspect only the declared paths through the no-follow reader, independent of UI pagination.
        from .studio_service import StudioError, _read_file
        public = service._workspace(path, thread).relative_to(path)
        missing = []
        for name in required:
            try:
                first, _ = _read_file(path, (public / name).as_posix(), 1)
                if not first:
                    missing.append(name)
            except StudioError as error:
                if error.code not in {"not_found", "invalid_path"}:
                    raise
                missing.append(name)
        drafted = set().union(*(item["files"] for item in candidates))
        states = {"completed": "已验收通过", "unverified": "已结束，尚未验证", "failed": "未通过整体验收",
                  "cancelled": "已取消", "running": "正在执行", "queued": "正在排队", "waiting_user": "正在等待答复",
                  "interrupted": "执行已中断"}
        status = summary["status"]
        title = (meta.get("title") or "").splitlines()[0] if meta.get("title") else path.name
        if len(title) > 90:
            title = title[:89] + "…"
        lines = [f"本群任务「{title}」{states.get(status, status)}。"]
        for item in candidates:
            lines.append(f"{item['role']} 已提交冻结草稿 `{item['version'][:12]}`。")
        for item in reviews:
            verdict = _VERDICTS[item["verdict"]]
            lines.append(f"{item['role']} 已签署{verdict}，对应版本 `{item['version'][:12]}`，共 {item['criteria']} 项。")
        if missing:
            for label, files in (("已提交草稿，仍待公开发布", [n for n in missing if n in drafted]),
                                 ("尚缺交付文件", [n for n in missing if n not in drafted])):
                if files:
                    lines.append(label + "：" + "、".join(f"`{name}`" for name in files) + "。")
        elif required:
            lines.append("已找到全部约定交付文件。")
        return {"source_run_id": path.name, "state": status, "candidates": candidates, "reviews": reviews,
                "pending_files": missing, "answer": "\n\n".join(lines)}
    return {"source_run_id": None, "state": "empty", "candidates": [], "reviews": [], "pending_files": [],
            "answer": "本群还没有可查询的执行任务。"}


def previous_status(service, path, request_id, text, fingerprint=None, *, attachments=False, reserve=False):
    from .studio_service import StudioError
    for event in reversed(service.room_events(path.name)):
        if event.get("request_id") != request_id:
            continue
        if event["kind"] != "status_reply" and not reserve:
            continue
        if (not attachments and event["kind"] == "status_reply" and event["question"] == text
                and event.get("fingerprint") == fingerprint):
            return event
        raise StudioError("同一请求 ID 不能用来发送不同消息。", "conflict", 409)
    return None
