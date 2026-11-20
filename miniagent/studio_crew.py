"""Delegate private working copies and coordinate from their public handoffs."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from collections import Counter, deque
from dataclasses import asdict, dataclass, replace
from functools import cached_property, partial
from pathlib import Path

from .acceptance import _relative_name
from .agent import Agent
from .context_window import estimate_tokens
from .crew_diagnostics import RoomDiagnostics
from .crew_progress import MemberProgress, _cover
from .member_processes import managed_member_processes
from .peer_recheck import ClosingPeerChecks
from .project_data import PROJECT_CACHE_DIRS, SQLITE_EXTENSIONS, supported_project_file, validate_project_data
from .rao import BoundHandoff, Budget, CONTROL_TOOLS, ErrorClass, Observation, PendingPeerReview, StepPolicy, ToolCompletion, policy_messages
from .sandbox import Sandbox
from .schemas import ToolResult
from .studio_acceptance import build_checks
from .studio_reviews import CandidateLookupError, InheritedCriteriaConflict, Review, ReviewQueue, ReviewRequest, ReviewSession, Rule, digest, manifest_issue
from .studio_conversations import SnapshotError, UnsupportedProjectFile, copy_project, member_profiles, snapshot_location
from .tools import build_default_registry


_KNOWLEDGE_READS = frozenset({"recall_role_knowledge", "read_role_knowledge"})
_PROJECT_READS = frozenset({"find_files", "list_dir", "search_files", "read_file", "read_lines"})
_INCIDENTAL_DATA = SQLITE_EXTENSIONS | {".log", ".csv", ".tsv", ".jsonl"}


@dataclass
class _ReadAllowance:
    max_tokens: int | None
    max_seconds: float
    max_rounds: int
    tokens: int = 0
    seconds: float = 0.0
    rounds: int = 0

    @property
    def exhausted(self):
        return ((self.max_tokens is not None and self.tokens >= self.max_tokens)
                or self.seconds >= self.max_seconds or self.rounds >= self.max_rounds)

    def record(self, tokens, seconds):
        self.tokens += tokens
        self.seconds += seconds
        self.rounds += 1


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _clip(text, limit):
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:max(limit - 3, 0)].decode("utf-8", "ignore") + "…"


def _manifest(staged):
    # Read only private, host-validated copies returned by copy_project.
    return {p.relative_to(staged).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in staged.rglob("*") if p.is_file()}


class CandidateAcceptanceError(SnapshotError):
    """A current, valid snapshot still fails a user-specified requirement."""


class RoomCrew:
    def __init__(self, run_id, path, thread, history, provider, settings, tracer,
                 service, should_cancel, human_handler, record_event, room_events, extensions, child_checks,
                 delivery=None, *, role_session=None):
        self.role_session = role_session
        self.inherited_review = None
        self.run_id = run_id
        self.path = path
        self.thread = thread
        self.history = history
        self.lead = thread["recipient"]
        self.profiles = member_profiles(thread)
        self.provider = provider
        self.settings = settings
        self.tracer = tracer
        self.service = service
        self.should_cancel = should_cancel
        self.human_handler = human_handler
        self.record_event = record_event
        self.room_events = room_events
        self.extensions = extensions
        self.budget = Budget.from_limits(settings.limits)
        self.deadline = time.monotonic() + self.budget.seconds_left
        self.handoffs = {}
        self.reports = []
        self.diagnostics = RoomDiagnostics(path, settings.api_key)
        self.prior_diagnostics = []
        self.assigned = set()
        self.project_version = 0
        self.assignment_results = {}
        self.private_drafts = {}
        self.covered_roles = set()
        self.untapped_shares = {}
        self.started_roles = set()
        self.refused_retry = set()
        self.dispatch_ended = ""
        self.routing_denied = False
        self.invalid_route = None
        self.child_checks = child_checks
        self.delivery = delivery or {}
        self.check_delivery = build_checks(self.delivery.get("files", []), self.delivery.get("checks", []))
        self.delivery_issue = ""
        self.pending_check_reviews = {}
        self.reviewer_probe_reviews = {}
        self.native_reviews = ReviewQueue(self.delivery.get("required_source_review_paths", []))
        if self.native_reviews.required_source_paths and thread.get("inherit_files", True):
            for turn in history:
                events = self.room_events(turn["run_id"]) if turn.get("events_truncated") else turn.get("events", [])
                for event in events:
                    if event.get("kind") in {"member_report", "direct_delivery"} and event.get("published"):
                        handoff = event.get("handoff", {})
                        author = (handoff.get("promoted_candidate") or {}).get("author", handoff.get("role"))
                        self.native_reviews.record_author(author, handoff.get("changed_files", []))
        self.approved_roles = set()
        self.planned = None
        self.plan_order = []
        self.routed_repairs = set()
        self.last_dispatch_error = ""
        self.close_tokens = 0
        self.close_seconds = min(90, self.budget.seconds_left * .12)
        self.close_state_bytes = 8000 if self.budget.tokens_left is None else min(8000, max(1200, self.budget.tokens_left // 16))
        self.inventory = self._inventory()
        self.private_read = ""
        self.navigation = []
        self.navigation_seen = set()
        self.navigation_ranges = {}
        self.navigation_recent = deque(maxlen=24)
        self.navigation_version = self.project_version
        self.inspection_revision = None
        self.inspection_decision = None
        self.inspection_notified = False
        self.nested_seconds = 0.0
        self.read_only = thread.get("intent") == "question"
        self.direct = bool(thread.get("direct_execution"))
        self.direct_baseline = {}
        self.direct_candidate = None
        if self.direct:
            if not self.read_only:
                copy_project(path, self.lead, None, extensions, secret=settings.api_key, manifest=self.direct_baseline)
            required = set(self.delivery.get("files", []))
            required.update(check["path"] for check in self.delivery.get("checks", []))
            self.direct_progress = MemberProgress(path / "agents" / self.lead, required)

    def direct_instructions(self):
        if self.read_only:
            return ("你是本轮问题的收件人。请查阅项目后直接在群里回答；需要其他专业意见时可以用 "
                    "delegate_task 邀请一位成员查阅资料，取得其回答后汇总。可邀请角色：" + _json(self.delegate_recipients))
        instructions = ("你是用户点名的执行者。请自己使用工作工具完成本轮并直接回复用户；无须先委派给自己。"
                "确实需要其他专业能力或独立复核时，用 delegate_task 邀请对应成员并说明具体目标。"
                "已有成稿可用 review.candidate 填项目路径，附 criteria，由另一成员核对实际版本。"
                "群内可邀请的角色：" + _json([{**v, "prompt": _clip(v["prompt"], 1200)} for v in self.profiles.values()]))
        if self.native_reviews.required_source_paths:
            instructions += ("\n本轮必须独立核查来源的成稿：" + _json(sorted(self.native_reviews.required_source_paths))
                + "。你可以亲自查阅原始来源、修改文件。准备好候选后用 delegate_task 发起正式评审："
                  "review.candidate 填成稿路径，criteria 包含必需的 source 标准。原作者以外的评审者须亲自读成稿、"
                  "查原始来源并签署当前版本。如果成稿由其他成员发布，也可以将这次独立评审交给你自己的成员角色。")
        if self.child_checks:
            instructions += "\n这些成员各自需要通过明确的子任务验收，请委派对应成员履行：" + _json(self.child_checks)
        return instructions

    def snapshot_direct_changes(self):
        """Freeze the addressed author's bytes before handing them to a peer or ending the turn."""
        if self.read_only:
            return
        current = {}
        copy_project(self.path, self.lead, None, self.extensions, secret=self.settings.api_key, manifest=current)
        if current == self.direct_baseline:
            return
        with tempfile.TemporaryDirectory(dir=self.path) as staging:
            stage = Path(staging) / "receipt"
            stage.mkdir()
            frozen = {}
            copy_project(self.path, self.lead, stage / "project", self.extensions, secret=self.settings.api_key, manifest=frozen)
            baseline = self.direct_baseline
            changes = [{"path": p, "change": "modified" if p in baseline else "added", "sha256": frozen[p]}
                       if p in frozen else {"path": p, "change": "deleted"}
                       for p in sorted(baseline.keys() | frozen.keys()) if baseline.get(p) != frozen.get(p)]
            if not changes:
                return
            (stage / "manifest.json").write_text(_json(frozen), encoding="utf-8")
            key = uuid.uuid4().hex
            artifacts = [{"path": f"handoffs/{key}/{c['path']}", "project_path": c["path"], "name": Path(c["path"]).name,
                          "size": (stage / "project" / c["path"]).stat().st_size, "sha256": c["sha256"]}
                         for c in changes if c["change"] != "deleted"]
            (self.path / "handoffs").mkdir(exist_ok=True)
            stage.rename(self.path / "handoffs" / key)
        owned = {p for p, (task, sha) in self.native_reviews.path_candidates.items()
                 if task == self.run_id and frozen.get(p) == sha}
        owned.update(a["project_path"] for a in artifacts)
        report = {"role": self.lead, "task_id": self.run_id, "objective": self.service.store.get(self.run_id).objective,
                  "published": True, "deliverable_paths": sorted(owned), "changed_files": changes}
        self.direct_candidate = report
        self.native_reviews.published(report, frozen, changes)
        for review in self.native_reviews.items.values():
            if review.state == "repair" and review.author == self.lead and not review.draft_task:
                edited = set(review.files).intersection(c["path"] for c in changes)
                if edited:
                    review.repaired(frozen, edited)
        self.direct_baseline = frozen
        self.project_version += 1
        self.inventory = self._inventory()
        self.record_event(self.run_id, self.lead, "direct_delivery", "定向执行者已保存本轮改动的版本。",
                          published=True, task_id=self.run_id, handoff=report, artifacts=artifacts)

    @cached_property
    def capabilities(self):
        ordinary_tools = "、".join(s.name for s in build_default_registry().specs())
        return ("成员工作阶段始终使用实际注册的工具：" + ordinary_tools +
                             "。可以读取现有文件、定位和修改实现、运行 Python 3 并测试。"
                             "缺少报告会产生进度提醒。最终文字交接和负责人最终汇总不调用工具。")

    def _inventory(self):
        root = self.path / "agents" / self.lead
        files = []
        for folder, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in PROJECT_CACHE_DIRS)
            for name in sorted(names):
                path = Path(folder) / name
                if (not name.startswith(".") and supported_project_file(name, self.extensions)
                        and not path.is_symlink() and path.is_file()):
                    files.append({"path": str(path.relative_to(root)), "bytes": path.stat(follow_symlinks=False).st_size})
        required = set(self.delivery.get("files", [])) | self.native_reviews.required_source_paths
        required.update(check["path"] for check in self.delivery.get("checks", []))
        return {"base_run_id": self.thread.get("base_run_id"), "revision": self.project_version,
                "total": len(files), "files": files[:64],
                "missing_acceptance_files": sorted(required - {f["path"] for f in files})}

    def instructions(self):
        roster = [{**v, "prompt": _clip(v["prompt"], 1200)} for v in self.profiles.values()]
        if self.read_only:
            return ("这是只读群聊。根据用户问题和成员专长，用 delegate_task 邀请成员查阅资料并给出意见。"
                    "把需要的后续成员填入 remaining_roles；收齐实际答复后直接在群内汇总。群成员与职责："
                    + _json(roster) + "。用户本轮点名的成员：" + _json(self.thread.get("addressed_roles", [])))
        return ("你是群负责人 " + self.lead + "。群成员和职责：" + _json(roster) +
                "。本轮可选 assignee ID：" + _json(self.delegate_recipients) + "。"
                "\n负责人也可以执行具体分工。用户或任务给你本人安排了调研、撰写或修订文件时，"
                "用 delegate_task，assignee 填你自己的 ID，deliverable_paths 列出你的文件；宿主会为你开启可写的独立成员任务。"
                "将其他成员的工作留给相应角色，独立验收交给不同的成员。"
                "\n" + self.capabilities + " 这份清单来自执行成员的工具注册表。成员可检索并读取公开来源时，请委派调查；来源能否访问以实际回执为准。" +
                "\n依据用户目标和角色职责选人。首次 delegate_task 须用 remaining_roles 列出这次后续需要的角色 ID，"
                "一人能完成时填 []。按任务需要邀请协作者；需要独立复核时，交给实际作者之外的成员。"
                "复核继承的成稿时可以直接用 delegate_task.review.candidate 填成稿路径、原作者任务 ID 或唯一作者角色，"
                "由另一成员独立检查现版。无需先让作者改写原件或重复交接。"
                "独立评估已交付的候选或带 reviewable_candidate 的完整私有草稿时，使用 delegate_task.review 引用原任务或作者角色，填写逐项标准；"
                "检查运行行为用 execute；审阅文件用 inspect。按本地原始记录核对成稿时用 inspect，并把原记录工作区路径写入每项相应标准的 reference_path。只有核实外部网页事实才用 source，需要评审员亲自打开网页。要求独立调查时，评审者可打开其他原始资料；检索未命中只说明已查范围。宿主会将返工转回适当成员。"
                "每次只委派给一人。按成员职责划出本阶段可以独立完成的范围。把本阶段必须落盘的产品或用户指定文件放在 deliverable_paths。宿主自动保存检查与交接回执；团队内部汇报直接写入交接消息。"
                "验收只列该成员负责的工作。其他成员的独立验证、交付说明和打包工作请留给其实际负责人，"
                "不要把整个项目的全部最终验收原样转给执行第一阶段的人。下一人的分工基于实际交接。"
                "分派分析、审阅或测试报告时，用 deliverable_paths 指定报告。即使交接受阻，也可读其诊断并按实证分工。"
                "同一人可以续修未发布的草稿。若同一人原地踏步，可点名另一位成员，用 recover_from 显式接手缺文件的有效草稿。"
                "成员完成且用户要求得到满足时立即交付。"
                "团队内选谁、补交、返工、是否由负责人读源码，均由你自主决定。"
                "开始委派前可核实公开项目中影响选人和分工的必要细节。收到成功交接后根据公开报告安排后续工作。"
                "工具回执中的调用次数只表示已经执行的次数。查询平台工具配置属于 runtime_capability。"
                "仅当用户掌握的业务需求、偏好、数据或权限确实缺失时 ask_human，并说明 decision_kind。"
                + ("\n用户要求独立核查指定成稿。继承的成稿可直接引用路径。若为本轮新增或修订，作者用 deliverable_paths 列出最终文件。委派另一成员，"
                 "用 delegate_task.review 引用作者的最新候选，列出必需的 source 标准。逐条核对重要主张，主动找反证；"
                 "区分正式规则和建议，注明研究样本与结论适用范围。核对笔记或旧版本不能代替对成稿的评审。"
                 if self.native_reviews.required_source_paths else "") +
                "\n用户指定的最终验收：" + _json(self.delivery) +
                "\n用户指定的成员验收：" + _json(self.child_checks))

    def human_request_gate(self, payload):
        question = str(payload.get("question", ""))
        kind = payload.get("decision_kind")
        # A user's permission or choice of deployment environment must remain
        # separate from the tools this host already assigned to room members.
        asks_about_host = re.search(
            r"(?i)(?:成员|负责人|本轮|本次会话).{0,35}(?:是否具备|是否有能力|能否使用|有没有).{0,55}(?:run_python|运行\s*Python|执行\s*shell)"
            r"|(?:does|do|can) (?:the |this |our )?(?:agent|member|worker|lead)s? .{0,25}(?:have access to|use|run) (?:the )?(?:run_python|python|shell)",
            question, re.S)
        if kind == "runtime_capability" or (kind != "external_permission" and asks_about_host):
            self.routing_denied = True
            self.last_dispatch_error = (self.capabilities + "负责人可委派和协调。具体资源的状态以实际执行回执为准。"
                                        "业务决定和外部授权仍须向用户询问。")
            return self.last_dispatch_error
        peer_names = "|".join(re.escape(role) for role in self.profiles if role != self.lead)
        internal = re.search(
            r"(?i)\b(?:re[- ]?delegat(?:e|ed|ing)|delegate_task)\b"
            rf"|\b(?:delegate|assign)\b.{0,65}\b(?:teammate|agent|{peer_names})\b"
            r"|\b(?:which|what) (?:agent|teammate|team member)\b.{0,50}\b(?:debug|fix|implement|review)\b"
            r"|\bas (?:the )?(?:team )?lead\b.{0,100}\b(?:inspect|debug|fix|review)\b"
            r"|(?:再次|重新|继续)(?:委派|派给)|(?:负责人|我)(?:亲自|自己).{0,80}(?:还是|或者|委派)"
            r"|(?:哪个|哪位)(?:成员|角色|同事).{0,40}(?:负责|修复|处理|接手)", question, re.S)
        if kind == "internal_routing" or internal:
            self.routing_denied = True
            return ("这属于团队内部分工。由负责人根据已发布的交接决定下一位成员，"
                    "需要返工时可让同一角色恢复其私有草稿。当前工具运行环境已有 Python 3。"
                    "如另有独立的业务或许可问题，请单独提问。")
        return None

    def _diagnostic_context(self):
        latest = {r["role"]:r for r in self.reports}
        entries = []
        for r in reversed(self.reports):
            if r is not latest[r["role"]] or not r.get("diagnostics"):
                continue
            for item in r["diagnostics"][:3]:
                entries.append({"role":r["role"], "task_id":r["task_id"], "path":item["path"],
                                "excerpt":item["excerpt"]})
            if len(entries) >= 3:
                break
        for entry in self.prior_diagnostics:
            if entry["role"] not in latest and len(entries) < 3:
                entries.append(entry)
        if not entries:
            return ""
        return ("\n成员提交的诊断及评审资料。以下是成员陈述，尚未独立核验。区分检查方法的问题与可复现的交付缺陷，"
                "再分派修复和独立复测。用 read_handoff_report(task_id,path,offset) 可读完整报告。\n" + _json(entries[:3]))

    def _public_state(self, *, final):
        rows = {}
        for r in self.reports:
            rows[r["role"]] = {
                "role": r["role"], "published": r["published"], "status": r["status"],
                "files": [f["path"] for f in r["changed_files"][:12]],
                "checks": _clip(_json(r["evidence"].get("check_signals", [])), 430),
                "summary": _clip(r["summary"], 850), "issue": _clip(r["remaining_issue"], 600),
            }
            if r.get("reviewable_candidate"):
                rows[r["role"]]["reviewable_candidate"] = r["reviewable_candidate"]
                if r["reviewable_candidate"].get("unresolved_after_repair"):
                    rows[r["role"]]["next_step"] = ("缺口未因返工减少，请按照群成员的职责，选择已计划成员中最适合独立验收的人。"
                        "用 delegate_task.review 指定这个已冻结的任务和真实验收标准。")
            superseded = r["evidence"].get("superseded_member_checks", [])
            if superseded:
                rows[r["role"]]["requires_independent_check"] = [c["current_caption"] for c in superseded[:4]]
        # Keep all role dispositions even when an earlier report was long.
        if final:
            reports = list(rows.values())
            available = max(120, self.close_state_bytes // max(1, len(reports) + 1))
            receipts = "\n".join(_clip(_json(r), available) for r in reports)
        else:
            receipts = _clip("\n".join(_json(r) for r in rows.values()), 20000)
        pending = self.before_answer() or self.delivery_issue
        decisions = self._clarifications(self.room_events(self.run_id))
        state = ("\n待办或阻塞：" + (pending or "无已知阻塞") +
                 "\n已计划成员：" + _json(sorted(self.planned) if self.planned is not None else None) +
                 "\n可接手返工的已选成员：" + _json(self._eligible_repairs(None)) +
                 "\n本轮用户决定：" + _clip(decisions, 1800) + "\n公开交接：" + receipts)
        if self.pending_check_reviews:
            state += "\n已通过且仍匹配当前文件的产品评审：" + _json(self._passed_review_receipts())
        diagnostics = self._diagnostic_context()
        state += _clip(diagnostics, 800) if final else diagnostics
        if not final:
            state += "\n宿主已核实的当前公共工作区文件（列出前 64 个，未发布草稿不影响该清单）：" + _json(self.inventory)
        if self.last_dispatch_error:
            state += "\n最近的委派反馈：" + self.last_dispatch_error
        return state

    def _prepare_inspection(self, budget):
        # Retain a cumulative ceiling on the same canonical bytes across
        # unsuccessful handoffs. Each new dispatch decision receives a
        # smaller allowance for looking into genuinely new information.
        available = budget.available_tokens(self.close_tokens)
        seconds = max(.1, budget.seconds_left - self.close_seconds)
        steps = None if budget.steps_left is None else max(1, budget.steps_left - 2)
        if self.inspection_revision != self.project_version:
            self.inspection_revision = self.project_version
            self.revision_reads = _ReadAllowance(None if available is None else max(1024, math.ceil(.03*available)),
                                                 max(1.0, .045*seconds), 9 if steps is None else max(2, math.ceil(.09*steps)))
            self.inspection_decision = None
        decision = (self.project_version, len(self.reports))
        if self.inspection_decision != decision:
            self.inspection_decision = decision
            self.decision_reads = _ReadAllowance(None if available is None else max(768, math.ceil(.015*available)),
                                                 max(.5, .018*seconds), 5 if steps is None else max(2, math.ceil(.05*steps)))
            self.inspection_notified = False
        self.nested_seconds = 0.0

    def _reset_navigation_revision(self):
        if self.navigation_version == self.project_version:
            return False
        self.navigation_version = self.project_version
        self.navigation.clear()
        self.navigation_seen.clear()
        self.navigation_ranges.clear()
        self.navigation_recent.clear()
        return True

    def _navigation_outcome(self, obs, path, report_task):
        if not obs.ok:
            return "failed"
        meta = obs.meta
        version = meta.get("sha256")
        start = meta.get("offset")
        end = None
        if obs.source == "read_lines":
            start, end = meta.get("char_start"), meta.get("char_end")
        elif obs.source == "read_file" and type(start) is int and type(meta.get("returned_chars")) is int:
            end = start + meta["returned_chars"]
        elif obs.source == "read_handoff_report" and type(start) is int:
            try:
                page = json.loads(obs.content)
            except (ValueError, RecursionError):
                page = None
            if isinstance(page, dict) and isinstance(page.get("content"), str):
                end = start + len(page["content"])
        if version and type(start) is int and type(end) is int and 0 <= start <= end:
            # read_file and read_lines share a character coordinate system.
            # Handoff reports belong to their own immutable task and file.
            ranges = self.navigation_ranges.setdefault((report_task, path, version), [])
            return "new" if _cover(ranges, start, end) else "covered"
        signature = (obs.source, report_task, path, hashlib.sha256(obs.content.encode()).digest())
        if signature in self.navigation_seen:
            return "covered"
        self.navigation_seen.add(signature)
        return "new"

    def _navigation_feedback(self):
        outcomes = Counter(status for _, status in self.navigation_recent)
        if outcomes["covered"] < 2:
            return ""
        repeats = Counter(key for key, status in self.navigation_recent if status == "covered")
        targets = []
        for (tool, path, task), count in repeats.most_common(8):
            targets.append({"tool": tool, "path": path, "repeat": count, **({"task": task} if task else {})})
        reviews = [{"target": r.target, "state": r.state, "reviewer": r.reviewer}
                   for r in self.native_reviews.items.values() if r.state != "passed"]
        for item in self.reviewer_probe_reviews.values():
            if not item.get("resolved"):
                review = item["review"]
                reviews.append({"target": review.target, "state": review.state, "reviewer": review.reviewer})
        facts = {"公开版本": self.project_version, "最近查询": len(self.navigation_recent),
                 "读到新范围或新结果": outcomes["new"], "已覆盖或相同结果": outcomes["covered"],
                 "查询失败": outcomes["failed"], "重复命中": targets,
                 "公共区缺少的验收文件": self.inventory["missing_acceptance_files"],
                 "本轮已实际委派角色": sorted(self.started_roles),
                 "未结独立评审": reviews, "待独立复核的检查任务": sorted(self.pending_check_reviews),
                 "缺少独立来源签署的文件": self.native_reviews.missing_source_reviews()}
        return ("\n分工提醒：这是同一公开版本近期查询的实测记录。部分重叠但读到新字符的页面计入新范围。"
                "请结合缺失文件、真实交接及未结复核，选择继续查阅、委派具体工作或说明阻塞。\n" + _json(facts))

    def observe_coordinator(self, record, state):
        """Keep attributable public tool receipts in the next compact request."""
        if self.direct:
            self.direct_progress.observe(record, state)
            return
        if self._reset_navigation_revision():
            # A member may publish during this same tool batch.
            return
        observations = [o for o in record.observations
                        if o.source in _PROJECT_READS | _KNOWLEDGE_READS | {"read_handoff_report"}]
        if not observations:
            return
        # Step latency includes synchronous member runs; only the lead's own
        # reading turn belongs here. A mixed human question can wait for the
        # user, so that batch contributes tokens/rounds but no elapsed time.
        elapsed = max(0.0, record.latency_ms/1000 - self.nested_seconds)
        if any(o.source == "human" for o in record.observations):
            elapsed = 0.0
        for allowance in (self.revision_reads, self.decision_reads):
            allowance.record(record.total_tokens, elapsed)
        args = {}
        last = next((m for m in reversed(state.history) if m.get("role") == "assistant"), {})
        for call in last.get("tool_calls") or []:
            try:
                args[call["id"]] = json.loads(call["function"]["arguments"])
            except (KeyError, ValueError, TypeError):
                continue
        for obs in observations:
            parameters = args.get(obs.tool_call_id, {})
            if not isinstance(parameters, dict):
                parameters = {}
            body = obs.content
            if obs.source in _KNOWLEDGE_READS:
                # Only the lead's next compact prompt may see this receipt.
                self.private_read = body if len(body.encode()) <= 12000 else _clip(body, 8500) + " … " + body[-800:]
                continue
            path = obs.meta.get("path") or parameters.get("path")
            path = os.path.normpath(path) if isinstance(path, str) and path else "."
            task = parameters.get("task_id") if obs.source == "read_handoff_report" else None
            if not isinstance(task, str):
                task = None
            outcome = self._navigation_outcome(obs, path, task)
            self.navigation_recent.append(((obs.source, path, task), outcome))
            excerpt = _clip(body, 1800)
            if len(body.encode()) > 1800:
                excerpt += " … " + _clip(body[-280:], 400)
            entry = {"tool": obs.source, "path": path, "ok": obs.ok, "excerpt": excerpt}
            for key in ("query", "pattern", "start_line", "offset"):
                if key in parameters:
                    entry[key] = _clip(str(parameters[key]), 180)
            self.navigation.append(entry)
        del self.navigation[:-5]

    @cached_property
    def delegate_recipients(self):
        return list(self.profiles)

    @cached_property
    def delegate_spec(self):
        original = CONTROL_TOOLS["delegate_task"]
        properties = dict(original.parameters["properties"])
        properties["assignee"] = {"type": "string", "description": "本次分工的角色 ID，须取枚举中的一个。",
                                  "enum": self.delegate_recipients}
        remaining = properties["remaining_roles"]
        properties["remaining_roles"] = {**remaining, "items": {"type": "string", "enum": self.delegate_recipients}}
        description = "将明确的子任务交给指定群成员。负责人承担文件交付时，用自己的 ID 和具体交付路径认领。"
        if self.read_only:
            description = "邀请群成员只读查阅项目并给出意见。"
            for edit_parameter in ("deliverable_paths", "review", "recover_from"):
                properties.pop(edit_parameter, None)
        return original.model_copy(update={
            "description": description,
            "parameters": {**original.parameters, "properties": properties,
                           "required": [*original.parameters["required"], "assignee"]},
        })

    @cached_property
    def coordinator_schemas(self):
        registry = build_default_registry(read_only=True)
        registry.add(self.diagnostics.tool())
        if self.role_session:
            registry = self.role_session.registry(self.lead, registry, read_only=True)
        specs = [*registry.specs(), *CONTROL_TOOLS.values()]
        return {s.name:(self.delegate_spec if s.name == "delegate_task" else s).to_api() for s in specs}

    @staticmethod
    def _decisions(events):
        questions = {}
        pairs = []
        for event in events:
            request_id = event.get("request_id")
            if event["kind"] == "human_question" and request_id:
                questions[request_id] = event["text"]
            elif event["kind"] == "human_answer" and event.get("role") == "user" and request_id in questions:
                pair = {"question": _clip(questions[request_id], 320), "answer": _clip(event["text"], 800)}
                if pair not in pairs:
                    pairs.append(pair)
        return pairs

    def _handoff_decisions(self, limit):
        pairs = [p for turn in self.history for p in self._decisions(turn.get("events", []))]
        pairs.extend(self._decisions(self.room_events(self.run_id)))
        chosen = []
        for pair in reversed(pairs):
            row = {"question": _clip(pair["question"], min(200, limit // 3)),
                   "answer": _clip(pair["answer"], min(440, limit // 2))}
            if row not in chosen and len(_json([row, *chosen]).encode()) <= limit:
                chosen.insert(0, row)
                if len(chosen) == 4:
                    break
        return chosen

    def _prior_context(self, *, include_model_answers=True):
        if not self.history:
            return ""
        recent = []
        decisions = []
        for turn in self.history:
            decisions.extend(self._decisions(turn.get("events", [])))
        for turn in self.history[-2:]:
            row = {"user": _clip(turn["user"], 800), "status": turn["status"]}
            # An unsuccessful attempt often reports a guess about tools or
            # missing files. The host inventory and actual receipts supersede it.
            if include_model_answers and turn["status"] in {"completed", "unverified"}:
                row["answer"] = _clip(turn["answer"], 1000)
            receipts = []
            for event in turn.get("events", []):
                if event["kind"] == "member_report" and event.get("published"):
                    handoff = event.get("handoff", {})
                    receipts.append({"role": event["role"], "files":[f["path"] for f in handoff.get("changed_files", [])[:8]],
                                     "checks":handoff.get("evidence",{}).get("check_signals",[])[:3]})
            if receipts:
                row["published"] = receipts[-4:]
            recent.append(row)
        background = {"initial_request": _clip(self.history[0]["user"], 850),
                      "user_decisions": decisions[-4:], "recent": recent}
        return "\n历史用户要求、明确答复和已发布回执（当前任务和工作区清单优先）：" + _json(background)

    def _member_context(self, role, group_goal, expectations, required, spec, repair, review=None):
        # Work turns have their own source tree. Prior model answers about
        # missing tools or old files are not evidence about this assignment.
        files = self.inventory["files"]
        assigned = set(required)
        ordered = sorted(files, key=lambda f: (f["path"] not in assigned, f["path"]))
        peers = []
        for report in self.reports:
            if report["published"]:
                peers.append({"role": report["role"], "task": report["task_id"],
                              "summary": _clip(report["summary"], 450), "files": [f["path"] for f in report["changed_files"][:6]],
                              "checks": report["evidence"].get("check_signals", [])[:3]})
                superseded = report["evidence"].get("superseded_member_checks", [])
                if superseded:
                    peers[-1]["requires_independent_check"] = [c["current_caption"] for c in superseded[:4]]
        role_info = self.profiles[role]
        facts = {"role": {"id": role, "name": role_info["name"], "responsibility": role_info["prompt"]},
                 "group_goal": group_goal, "assignment_acceptance": expectations,
                 "assigned_files": required, "member_checks": spec, "team_checks": self.delivery,
                 "workspace": {"total_files": self.inventory["total"], "files": ordered[:24]},
                 "published_peers": peers[-3:]}
        reviews = self._review_requests(role, review)
        if reviews:
            facts["pending_independent_checks"] = [
                {k: v for k, v in item.items() if k in {"task", "author", "changes", "verification"}}
                for item in reviews]
            facts["review_instructions"] = self._review_instructions(reviews)
        current = self._decisions(self.room_events(self.run_id))[-4:]
        # Share source data seen on the current public revision; restored
        # private drafts can be compared to these original locations.
        landmarks = []
        if self.navigation_version == self.project_version:
            for row in reversed(self.navigation):
                if not row["ok"] or row["tool"] in {"list_dir", "find_files", "read_handoff_report"}:
                    continue
                location = {k: row[k] for k in ("tool", "path", "query", "start_line", "offset") if k in row}
                entry = {**location, "excerpt": _clip(row["excerpt"], 650)}
                if entry not in landmarks:
                    landmarks.append(entry)
                if len(landmarks) == 3:
                    break
        if landmarks:
            facts["public_source_locations"] = landmarks
        return ("你在本地项目副本中完成当前分工。可用工具以本轮接口为准。"
                "查阅公开网页或 PDF 请先用 http_get（可选 pdf_page），正式文本来源保存在项目的 sources/*.json。"
                "用 read_source(path, query 或 offset) 定位已存正文或检索记录中的证据。资料够写一节时，把带出处的结论写进本阶段文件，再继续补查。"
                "手动下载供解析的原始 PDF、压缩包及工具缓存须放在项目外的系统临时目录，"
                "例如在同一次 run_python 调用中使用 tempfile.TemporaryDirectory()。项目中保留交付物和来源记录。"
                "当前分工来自群负责人。如果分工指定的测试方法与验证要求冲突，向负责人交接冲突和证据；用户只决定业务偏好或外部授权。"
                "请按实际文件和测试结果交接成果与限制，仍有阻塞时以 [BLOCKED] 说明原因。"
                "\n当前分工资料：" + _json(facts) + "\n本轮真实用户答复：" + _json(current)
                + self._diagnostic_context() + self._prior_context(include_model_answers=False) + ("\n" + repair if repair else ""))

    def _reviewed_delivery(self):
        """Receipts for the exact, published files in the user's delivery gate."""
        expected = set(self.delivery.get("files", []))
        expected.update(check["path"] for check in self.delivery.get("checks", []))
        if not expected or not self.native_reviews.items:
            return ()
        covered = set()
        receipts = []
        for item in self.native_reviews.items.values():
            if (item.state != "passed" or item.latest.get("candidate_version") != item.version
                    or any(self.native_reviews.public_versions.get(p) != sha for p, sha in item.files.items())):
                return ()
            files = {p: sha for p, sha in item.files.items() if p in expected and sha}
            if files:
                covered.update(files)
                receipts.append({"target": item.target, "reviewer": item.reviewer, "state": item.state,
                                 "candidate_version": item.version, "files": files,
                                 "required_criteria": [rule.id for rule in item.criteria if rule.required]})
        return tuple(receipts) if covered == expected else ()

    def _next_review(self):
        for item in getattr(self, "reviewer_probe_reviews", {}).values():
            if not item.get("resolved"):
                child = item["review"]
                child.resume_after_answer(self._review_answers())
                return child
        return self.native_reviews.next(self._review_answers())

    def _pending_probe(self):
        return next((r for r in getattr(self, "reviewer_probe_reviews", {}).values() if not r["resolved"]), None)

    @staticmethod
    def _bound_review_handoff(review):
        if not review or review.state not in {"review", "repair"} or not (review.latest or review.probes):
            return None
        objective = ("按原验收反馈修订冻结候选并重新交接。" if review.state == "repair"
                     else "对绑定的候选和未决标准独立取证并签署。")
        return BoundHandoff(review.target, review.version, review.state, review.owner, objective,
                            digest([len(review.visited), review.draft_task, review.peer_recheck]))

    def _probe_dispatch_hint(self, pending):
        if not pending:
            for product in self.native_reviews.items.values():
                if product.state == "review" and product.peer_recheck.get("candidate_version") == product.version:
                    return ("第三位已独立核对争议探针，原产品结论仍待重签。请调用 delegate_task，assignee=" + product.reviewer +
                            "，省略 review、recover_from，沿用目标 " + product.target + " 和版本 " + product.version +
                            "。原评审可根据 peer_recheck 的证据线索独立取证，在原标准中引用本次实际回执。")
            return ""
        child = pending["review"]
        if child.state == "blocked":
            return ("本轮独立裁决已经阻塞，重复委派不能解锁。请向用户报告目标 " + child.target + " 的具体原因：" +
                    (child.host_issue or "未取得符合标准的新证据或有效工件。"))
        if child.state != "review":
            return ""
        request = {"assignee": child.reviewer, "objective": "在同一冻结候选上补查未决原标准，提交独立裁决。",
                   "deliverable_paths": []}
        return ("独立裁决正在等待 " + child.reviewer + "，目标 " + child.target + "，候选 SHA-256 " + child.version +
                "。从负责人调用 delegate_task " + _json(request) + "，省略 review 和 recover_from。"
                "宿主会恢复独立成员的冻结副本。即使你本人就是该成员，也请从独立成员任务中签署。"
                "请保持公共工作区的当前版本，裁决结束后再编辑成稿和评审报告。")

    def guard_probe_inputs(self, registry):
        for name in ("write_file", "edit_file", "append_file", "delete_file"):
            if tool := registry.get(name):
                registry.add(replace(tool, fn=partial(self._guard_probe_input, name, tool.fn)))

    def _guard_probe_input(self, name, original, ctx, args):
        pending = self._pending_probe()
        if pending and pending["review"].state == "review":
            child = pending["review"]
            draft = pending.get("private_evidence") or self.private_drafts.get(child.author)
            if not draft or draft["task_id"] != child.draft_task or draft["version"] != self.project_version:
                return original(ctx, args)
            path = ctx.sandbox.resolve(args["path"])
            rel = ctx.sandbox.relative(path)
            protected = child.probe_product_files.keys() | set(pending["report"]["deliverable_paths"])
            if rel in protected:
                base = pending["base"].get(rel)
                same_write = name == "write_file" and hashlib.sha256(args["content"].encode()).hexdigest() == base
                same_edit = name == "edit_file" and args["old_text"] == args["new_text"]
                would_change = not (same_write or same_edit or name == "append_file" and not args["content"]
                                    or name == "delete_file" and not path.exists())
                # Do not obstruct restoring already-changed public bytes. The
                # normal checkpoint/base validation still runs before dispatch.
                if would_change and (not path.exists() and base is None or path.is_file() and base
                        and path.stat().st_size <= 8 * 1024**2 and hashlib.sha256(path.read_bytes()).hexdigest() == base):
                    return ToolResult.failure(f"修改裁决输入 {rel} 会使当前冻结候选失效。" + self._probe_dispatch_hint(pending))
        return original(ctx, args)

    @staticmethod
    def _bound_probe_conflicts(review, session, progress):
        """Only an accepted finding's explicit contrary evidence is disputed.

        Incidental exploratory failures do not acquire new product criteria.
        Missing source or another unresolved failure remains a hard blocker.
        """
        if (not session or not session.reviewer_task or not session.assessment or review.kind != "product" or progress.pending_paths()
                or session.snapshot_issue()):
            return {}
        verdict, results = session.assessment["verdict"], session.assessment["results"]
        unresolved = verdict == "inconclusive" and not session.verification_conflict_issue(results)
        if verdict != "passed" and not unresolved:
            return {}
        bound, _ = session.reviewer_failures(results, include_verification=unresolved)
        return bound

    def _queue_reviewer_probes(self, parent, report, draft, assessment, support):
        # Even an unavailable independent actor must not erase accepted signed
        # findings. State remains blocked/review until the obligation is resolved.
        parent.latest = assessment
        probes = draft["inline_failures"]
        if (not draft["checkpoint"] or not draft["manifest"]
                or set(probes) != set(draft["failed_checks"]) or len(probes) > 24):
            parent.host_issue = "失败检查的完整原始探针或冻结快照无法交付给独立同伴。请恢复原检查和输入后重新验证。"
            return False
        # The coordinator may adjudicate in an isolated member task when
        # someone else authored the product and someone else signed the probe.
        candidates = [*self.plan_order, *(p for p in self.profiles if p != self.lead), self.lead]
        peers = [p for p in dict.fromkeys(candidates) if p not in {parent.reviewer, parent.author}]
        if not peers:
            parent.host_issue = "原评审引用的失败检查需要独立于产品作者和检查作者的裁决者，当前没有合格成员；保留原失败证据，不能自审通过。"
            return False
        # The reviewer tree and its original product inputs are already frozen
        # by _remember_draft. Never recreate the old assertion from a summary.
        extra = self._transferable_probe_support(probes, support)
        implicated = {finding["criterion_id"] for finding in assessment["results"]
                      if any(r["kind"] == "failed_check" and r["scope"] in probes for r in finding["receipts"])}
        original_rules = [rule for rule in parent.criteria if rule.id in implicated]
        rules = []
        for scope in probes:
            ids = {f["criterion_id"] for f in assessment["results"]
                   if any(r.get("scope") == scope and r["kind"] == "failed_check" for r in f["receipts"])}
            affected = [rule for rule in original_rules if rule.id in ids]
            method = ("execute" if parent.draft_task or assessment["verdict"] == "inconclusive" or any(r.method == "execute" for r in affected) else
                      "source" if any(r.method == "source" for r in affected) else "inspect")
            rules.append(Rule(id="probe_" + scope, method=method, description=(
                "独立裁决原评审明确引用的失败检查 " + scope + "，说明原断言/真实错误为何与以下原业务标准冲突或一致：" +
                _clip(_json([rule.model_dump(exclude_none=True) for rule in affected]), 950))))
        # Keep the original inspection/source semantics as well as the exact
        # executable counterexample. A passing assertion alone cannot establish
        # an externally sourced product claim.
        rules.extend(Rule(id=("original_" + rule.id if len(rule.id) <= 71 else
                              "original_" + hashlib.sha256(rule.id.encode()).hexdigest()),
                          method=rule.method, description=rule.description, required=rule.required, reference_path=rule.reference_path)
                     for rule in original_rules if rule.method != "execute")
        if len(rules) > 24:
            parent.host_issue = "争议涉及的原标准和失败范围超过单次复核上限；保留全部证据，不能省略核销。"
            return False
        if not parent.files or any(draft["manifest"].get(p) != sha for p, sha in parent.files.items()):
            parent.host_issue = "争议复核缺少原产品完整冻结版本，不能签发裁决。"
            return False
        product_paths = {r["path"] for finding in assessment["results"]
                         if finding["criterion_id"] in implicated for r in finding["receipts"]
                         if r["kind"] == "read" and r.get("extent", 0) > 0}
        for finding in assessment["results"]:
            if finding["criterion_id"] not in implicated:
                continue
            for receipt in finding["receipts"]:
                if receipt["kind"] == "passed_check" and receipt.get("asserted"):
                    product_paths.update(path for path, sha in receipt.get("asserted_inputs", {}).items()
                                         if parent.files.get(path) == sha)
        for probe in probes.values():
            product_paths.update(path for path, sha in probe.get("program_inputs", {}).items()
                                 if parent.files.get(path) == sha)
        product_paths.update(parent.required_source_paths)
        if not product_paths:
            product_paths.update(self.delivery.get("files", []))
        product_files = {p: sha for p, sha in parent.files.items() if p in product_paths}
        comparison_targets = parent.compare_with(None) & product_files.keys()
        if not product_files:
            parent.host_issue = "争议缺少原业务标准对应的产品输入，无法把独立断言绑定到成稿。"
            return False
        reference_inputs = {}
        if assessment["verdict"] == "inconclusive":
            comparisons = {r.id:r.reference_path for r in original_rules if r.reference_path}
            for finding in assessment["results"]:
                if ref := comparisons.get(finding["criterion_id"]):
                    for receipt in finding["receipts"]:
                        if receipt["kind"] == "failed_check" and receipt["scope"] in probes:
                            reference_inputs.setdefault("probe_" + receipt["scope"], {})[ref] = draft["manifest"][ref]
        child = Review(report["task_id"], parent.reviewer, peers[0],
                       "复核评审者自建检查的争议；真实产品缺陷回原作者。原产品标准：" +
                       _clip(_json([r.model_dump(exclude_none=True) for r in parent.criteria]), 5000),
                       rules, dict(draft["manifest"]), draft_task=report["task_id"], probes=dict(probes),
                       kind="reviewer_probe", probe_parent=parent.target, probe_support=extra,
                       probe_product_files=product_files, comparison_targets=comparison_targets,
                       probe_reference_inputs=reference_inputs,
                       required_source_paths=(set(parent.required_source_paths)
                           if any(rule.method == "source" for rule in original_rules) else set()))
        self.reviewer_probe_reviews[child.target] = {
            "review": child, "parent": parent, "parent_version": parent.version,
            "assessment": assessment, "report": report, "base": dict(draft["base"]), "resolved": False}
        if parent.draft_task:
            self.reviewer_probe_reviews[child.target].update(private_evidence=draft,
                private_product=self.private_drafts[parent.author], disputed_criteria=sorted(implicated))
        parent.state = "review"  # Suspended behind the exact child; never reopened with new rules.
        parent.host_issue = "已签评审引用的失败检查待独立裁决：" + ", ".join(probes)
        report["reviewer_probe_review"] = {"target": child.target, "parent": parent.target,
            "candidate_version": child.version, "scopes": list(probes), "reviewer": child.reviewer}
        self.record_event(self.run_id, "studio", "reviewer_probe_pending", parent.host_issue,
                          task_id=report["task_id"], parent_target=parent.target, parent_version=parent.version,
                          candidate_version=child.version, reviewer=child.reviewer, probes=probes,
                          supporting_receipts=extra, accepted_assessment=assessment)
        return True

    def _transferable_probe_support(self, probes, support):
        extra = []
        remaining = 98304 - sum(len((p["code"] + p["error"]).encode()) for p in probes.values())
        for row in support:
            data = _json(row).encode()
            if len(data) > remaining:
                continue
            try:
                validate_project_data("probe-support.txt", data, secret=self.settings.api_key, reject_credentials=True)
            except ValueError:
                continue
            extra.append(row)
            remaining -= len(data)
        return extra

    def _queue_author_probes(self, review, report, draft, support, reviewer):
        """A corrected author check can freeze the draft for a peer, never publish it or clear a failure."""
        probes = draft["inline_failures"]
        support = self._transferable_probe_support(probes, support)
        if (self.should_cancel() or self.budget.exhausted or time.monotonic() >= self.deadline
                or not draft["checkpoint"] or not draft["manifest"] or draft["missing"] or not support or not probes
                or probes.keys() != draft["failed_checks"].keys() or report["role"] == reviewer):
            return False
        files = draft["manifest"]
        corrected = [s for s in support if s.get("kind") == "corrected_file_check"]
        file_scopes = {k for k, p in probes.items() if p.get("kind") == "edited_script"}
        matched = {scope for receipt in support for scope, call in receipt.get("original_calls", {}).items()
                   if scope in probes and probes[scope]["tool_call_id"] == call}
        if not probes.keys() <= matched:
            return False
        if file_scopes:
            frozen_version = digest(files)
            covered = {key for s in corrected if files.get(s["path"]) == s["current_sha256"] and s["input_version"] == frozen_version
                       for key in s["original_calls"]}
            if not file_scopes <= covered:
                return False
        if review is None:
            sources = set(files) & self.native_reviews.required_source_paths
            rules = [Rule(id="independent_recheck", method="execute", description=
                          _clip(report["acceptance_expectation"] or report["objective"], 1200))]
            if sources:
                rules.append(Rule(id="required_sources", method="source", description="核对用户指定文件中的核心事实及原始来源。"))
            review = Review(report["task_id"], report["role"], reviewer, report["objective"], rules, dict(files),
                            required_source_paths=sources)
        if (review.kind != "product" or review.author != report["role"] or review.reviewer != reviewer
                or review.probes or len(review.criteria) + len(probes) > 24
                or any("probe_" + key == rule.id for key in probes for rule in review.criteria)):
            return False
        # A localhost URL does not identify source bytes. When the candidate has
        # a single product HTML entry the independent browser check can compare
        # the actual HTTP response bytes to that frozen file.
        html = sorted(p for p in files if Path(p).suffix.lower() in {".html", ".htm"})
        probes = {key: ({**p, "input_paths": p.get("investigation_paths") or (html if len(html) == 1 else [])}
                        if p["tool"] == "run_browser" and not p.get("input_paths") else p) for key, p in probes.items()}
        targets = set(draft["required"])
        targets.update(p for probe in probes.values() for p in probe.get("input_paths", ()))
        product = {p: files[p] for p in targets if p in files}
        if not product:
            return False
        review.probes = dict(probes)
        review.probe_support = support
        review.probe_product_files = product
        review.files = dict(files)
        review.draft_task = report["task_id"]
        review.state = "review"
        review.host_issue = "候选已冻结，作者失败与后来检查均已留存，等待独立成员逐项签署。"
        review.criteria.extend(Rule(id="probe_" + key, method="execute", description=
            f"对照本次产品要求，独立执行能区分正确与错误行为的断言，核对原失败 {probe['tool_call_id']} 的原因。")
            for key, probe in probes.items())
        self.native_reviews.items[review.target] = review
        draft["reviewable"] = True
        report.setdefault("reviewable_candidate", {"task_id": report["task_id"],
                          "candidate_version": review.version, "files_total": len(files)})
        report["author_probe_review"] = {"target": review.target, "reviewer": reviewer,
            "candidate_version": review.version, "original_calls": {k:p["tool_call_id"] for k,p in probes.items()},
            "corrected_calls": [s["tool_call_id"] for s in support],
            "supporting_receipts": [{k:s[k] for k in ("tool_call_id", "original_calls", "input_version")}
                                    for s in support],
            "pending_verification": draft["evidence"].get("pending_verification", [])}
        if corrected:
            report["author_probe_review"]["corrected_scripts"] = [
                {k:v for k,v in s.items() if k not in {"prior_source", "prior_error", "output"}} for s in corrected]
        self.record_event(self.run_id, "studio", "author_probe_pending", review.host_issue,
                          task_id=report["task_id"], **report["author_probe_review"])
        return True

    def _private_arbitration_issue(self, pending):
        """Neither the QA checkpoint nor the original unpublished product may change during adjudication."""
        parent = pending["parent"]
        product = pending["private_product"]
        evidence = pending["private_evidence"]
        if (self.private_drafts.get(parent.author) is not product or product["task_id"] != parent.draft_task
                or any(draft["version"] != self.project_version for draft in (product, evidence))):
            return "原私稿或公共版本已变化，争议签署无法用于当前候选。"
        try:
            for source, role, expected in ((product["checkpoint"], product["role"], parent.files),
                                          (evidence["checkpoint"], evidence["role"], pending["review"].files),
                                          (self.path, self.lead, product["base"])):
                manifest = {}
                copy_project(source, role, None, self.extensions, secret=self.settings.api_key, manifest=manifest)
                if issue := manifest_issue(expected, manifest, exact=True, secret=self.settings.api_key):
                    return "争议绑定的私稿、评审检查点或公共基线已变化：" + issue
        except (SnapshotError, OSError) as exc:
            return "无法复核争议绑定版本：" + str(exc)
        return ""

    def _finish_reviewer_probes(self, child, assessment, published, issue, changes, review_task):
        item = self.reviewer_probe_reviews[child.target]
        parent = item["parent"]
        if parent.version != item["parent_version"]:
            child.state = "blocked"
            parent.host_issue = "原产品候选版本已变化，旧评审不能恢复。"
            return
        private = bool(item.get("private_product"))
        if private and (snapshot_issue := self._private_arbitration_issue(item)):
            child.state = "blocked"
            parent.host_issue = child.host_issue = snapshot_issue
            return
        if assessment and assessment["verdict"] == "passed" and (published or private and child.state == "passed" and not issue):
            if not private and any(self.native_reviews.public_versions.get(p) != sha for p, sha in parent.files.items()):
                child.state = "blocked"
                parent.host_issue = "公共工件与原评审版本不符，旧签署仍待核对。"
                return
            item["resolved"] = True
            if private and child.reviewer not in self.assigned:
                # This can complete a planned peer slot, never a separate
                # assigned publication or a user-specified member check.
                self.covered_roles.add(child.reviewer)
            if not private and item["assessment"]["verdict"] == "passed":
                parent.apply(item["assessment"], self._review_answers())
                parent.host_issue = ""
                original = item["report"]
                ledger = {**original["evidence"], "pending_verification": []}
                self._record_check_reviews(original["role"], original["task_id"], original["summary"],
                                          ledger, item["base"], changes, parent.target)
                resolution = "probe_corrected"
            else:
                # A private product, or a public inconclusive finding, still
                # needs a product decision from its original evaluator.
                earlier = (parent.peer_recheck.get("adjudicated_scopes", {})
                           if parent.peer_recheck.get("candidate_version") == parent.version else {})
                parent.state = "review"
                parent.host_issue = "独立成员已复验争议探针，请原评审按原标准对同一冻结产品重新取证签署。"
                parent.peer_recheck = {
                    "candidate_version":parent.version, "review_task":review_task, "evaluator":child.reviewer,
                    "reference_files":{r.reference_path:child.files[r.reference_path] for r in child.criteria if r.reference_path},
                    "findings":[{"criterion_id":r["criterion_id"], "reason":r["reason"],
                                 "receipt_ids":[e["receipt_id"] for e in r["receipts"]]} for r in assessment["results"]]}
                if private:
                    parent.peer_recheck["required_fresh_criteria"] = item["disputed_criteria"]
                    parent.peer_recheck["adjudicated_scopes"] = {
                        **earlier, **{scope: review_task for scope in child.probes}}
                resolution = "probe_corrected_pending_product_review"
        elif assessment and child.state == "repair" and not issue:
            # Review.apply routes required findings by cause. A verification
            # gap stays with this reviewer even when changes were requested.
            # Return the real defect to the original product repair/review path.
            parent.state = "repair"
            parent.host_issue = "独立复核确认产品缺陷：" + _clip(_json(assessment["results"]), 2500)
            item["resolved"] = True
            resolution = "product_defect"
        else:
            return  # Inconclusive, a new failed probe or a hard gate stays pending.
        self.record_event(self.run_id, "studio", "reviewer_probe_resolved", resolution,
                          task_id=child.target, parent_target=parent.target, parent_version=parent.version,
                          evaluator=child.reviewer, scopes=list(child.probes), assessment=assessment)

    def _passed_review_receipts(self):
        """Product approval stays visible while a peer checks an edited test."""
        expected = set(self.delivery.get("files", [])) | set(self.native_reviews.required_source_paths)
        expected.update(c["path"] for c in self.delivery.get("checks", []))
        return [{"target": r.target, "author": r.author, "reviewer": r.reviewer,
                 "candidate_version": r.version, "files": {p: sha for p, sha in r.files.items() if p in expected},
                 "candidate_file_count": len(r.files),
                 "required_criteria": [c.id for c in r.criteria if c.required], "state": "passed"}
                for r in self.native_reviews.items.values()
                if r.state == "passed" and r.latest.get("candidate_version") == r.version
                and all(self.native_reviews.public_versions.get(p) == sha for p, sha in r.files.items())]

    def _pending_check_briefing(self):
        rows = []
        for item in self.pending_check_reviews.values():
            excluded = {self.lead, item["author"], item.get("last_editor")}
            roles = sorted(set(self.profiles) - excluded)
            rows.append({"task": item["task"], "author": item["author"], "last_editor": item.get("last_editor"),
                         "verification": item.get("verification", []), "changes": item.get("changes", []),
                         "eligible_roles": roles,
                         "completion_marker": "[CHECKED " + item["task"] + "]"})
        return rows

    def _only_peer_checks_remaining(self):
        # Never substitute test review for a pending product criterion, member
        # assignment, source review or delivery gate.
        return (bool(self.pending_check_reviews) and bool(self._reviewed_delivery()) and not self.delivery_issue
                and self._next_review() is None
                and not self.native_reviews.missing_source_reviews()
                and any(r["published"] for r in self.reports)
                and not (((self.planned or set()) | self.assigned) - self.approved_roles - self.covered_roles
                         | (set(self.child_checks) - self.approved_roles)))

    def _peer_check_instruction(self, requests=None, *, for_member=False):
        dispatch = ("你的本次分工是按原需求独立复核以下检查；已通过的产品标准保持原结论。" if for_member else
                    "这是附属检查的独立复核，已通过的产品评审不需要重开。"
                    "请用 delegate_task 委派 eligible_roles 中的另一成员，省略 review 和 recover_from，"
                    "deliverable_paths 填 []。")
        return (dispatch + "对照原需求阅读当前脚本，按下列同一 scope 的命令完整运行，"
                "保留真实退出码和当前文件 SHA；不得改待复核脚本或候选来核销。"
                "确认后在交接写 completion_marker；未通过则如实交接缺口。待复核记录："
                + _json(self._pending_check_briefing() if requests is None else requests))

    def coordinator_policy(self, state, budget):
        bare_close = StepPolicy("finalize", allowed_tools=frozenset(), compact=True)
        self.close_tokens = estimate_tokens(policy_messages(state, bare_close)) + self.close_state_bytes + 2304
        probe_hint = self._probe_dispatch_hint(self._pending_probe())
        if self.direct:
            recovery = self.direct_progress.recovery_feedback()
            instruction = self.direct_instructions() + "\n" + self.direct_progress.steering()
            if probe_hint:
                instruction = probe_hint + "\n" + instruction
            if recovery:
                instruction += ("\n" + recovery + "\n当前阅读位置：" + _json(self.direct_progress.read_checkpoint())
                                + "\n本轮成员交接：" + _json([{k: r[k] for k in ("role", "task_id", "published",
                                                                                   "remaining_issue")} for r in self.reports[-4:]])
                                + "\n缺少独立来源签署的文件：" + _json(self.native_reviews.missing_source_reviews()))
            return StepPolicy("work", instruction, compact=bool(recovery))
        pending = self.before_answer() or self.delivery_issue or self.dispatch_ended
        authenticated = () if pending else self._reviewed_delivery()
        review_header = ""
        if authenticated:
            review_header = ("宿主记录：用户指定文件的当前版本均已取得独立验收通过回执。"
                             "成员个人 status 和较早的委派回执分别描述当时的分工。"
                             "按这些文件及标准汇总，仍可说明它们未覆盖的真实限制。\n"
                             + _clip(_json(authenticated), self.close_state_bytes // 2) + "\n")
        closing = StepPolicy("finalize", "请根据成员公开交接汇总交付，说明证据、使用方法和局限。\n"
                             + _clip(review_header + self._missing_delivery_notice() + self._public_state(final=True),
                                     self.close_state_bytes),
                             frozenset(), compact=True, reviewed_delivery=authenticated)
        # Fixed upper bound for the final receipt plus the root's actual goal,
        # system message and authenticated human replies, with framing/output.
        self._prepare_inspection(budget)
        plan_finished = self.planned is not None or set(self.profiles) - {self.lead} <= self.approved_roles
        if self.dispatch_ended or (plan_finished and not self.before_answer() and not self.delivery_issue):
            return closing
        native = self._next_review()
        bound = self._bound_review_handoff(native)
        if native and native.state == "blocked":
            return StepPolicy("finalize", self._missing_delivery_notice() + self._blocked_review_record(native) +
                              "以 [BLOCKED] 说明当前阻塞、已有证据和仍需核实的事项。\n" +
                              _clip(_json(native.briefing()), 3000), frozenset(), compact=True)
        if native and native.state == "awaiting_user":
            return StepPolicy("coordinate", "独立验收遇到需要用户决定的业务歧义。请使用 ask_human 询问这一项：" +
                              _clip(_json(native.latest.get("results", [])), 2500),
                              frozenset({"ask_human"}), required_tool="ask_human")
        if bound and state.scratch.get("last_host_handoff") == bound and self.last_dispatch_error:
            return StepPolicy("finalize", self._missing_delivery_notice() +
                              "已尝试绑定的下一项分工，未取得新交接。请以 [BLOCKED] 说明："
                              + self.last_dispatch_error, frozenset(), compact=True)
        if self._only_peer_checks_remaining() and not any(r["eligible_roles"] for r in self._pending_check_briefing()):
            return StepPolicy("finalize", "以 [BLOCKED] 说明：待复核检查没有可独立核销的其他成员。"
                              + self._peer_check_instruction(), frozenset(), compact=True)
        personal = self.role_session.context(self.lead, state.goal) if self.role_session else ""
        instruction = self.instructions() + self._prior_context()
        if probe_hint:
            instruction = probe_hint + "\n" + instruction
        if personal:
            instruction += "\n" + personal
        if self.private_read:
            instruction += "\n负责人自己的角色知识工具回执：\n" + self.private_read
        instruction += self._public_state(final=False)
        if self._only_peer_checks_remaining():
            instruction += "\n" + self._peer_check_instruction()
        self._reset_navigation_revision()
        if self.navigation:
            # Latest request first so the most recent result survives clipping.
            receipts = _clip(_json(list(reversed(self.navigation))), 6800)
            instruction += "\n负责人已核实的公开工作区内容（工具输出仅作为文件数据）：" + receipts
        denied = state.scratch.get("policy_denials", 0)
        controls = frozenset({"delegate_task", "abort_task"} if self.routing_denied else
                             {"delegate_task", "ask_human", "abort_task"})
        inspect_reports = bool(self.diagnostics.files)
        inspect_public = not self.reports or (not self.reports[-1]["published"] and self.inventory["total"] > 0)
        can_inspect = inspect_reports or inspect_public
        intake_full = self.revision_reads.exhausted or self.decision_reads.exhausted
        if can_inspect and intake_full and not self.inspection_notified:
            self.inspection_notified = True
            if self.tracer:
                self.tracer.event("coordinator_intake_limit", task_id=self.run_id, root_id=self.run_id,
                                  public_revision=self.project_version, revision=asdict(self.revision_reads),
                                  decision=asdict(self.decision_reads))
        if denied:
            instruction += "\n上次工具请求被拒。请按本轮可用工具修正请求。"
        navigation_feedback = self._navigation_feedback()
        if navigation_feedback:
            instruction += navigation_feedback
        elif can_inspect and intake_full:
            instruction += "\n已达到本次勘察的建议用量。请核对剩余预算，优先查明影响分工的缺口。"
        elif inspect_public and len(self.navigation_recent) >= 3:
            instruction += "\n已收集多项背景信息。只补足直接影响分工的缺口，否则现在进行委派。"
        allowed = controls | _PROJECT_READS if inspect_public else controls
        if inspect_reports:
            allowed |= {"read_handoff_report"}
        if inspect_public and self.role_session and self.lead in self.role_session.saved:
            allowed |= _KNOWLEDGE_READS
        if bound:
            allowed = controls
            instruction += ("\n宿主已登记下一项分工。调用 delegate_task，assignee 填 " + bound.owner
                            + "，省略 review 和 recover_from。目标 " + bound.target + "，状态 " + bound.state
                            + "。受委派的成员可查阅资料并核实具体问题。")
        decision = StepPolicy("coordinate", instruction, frozenset(allowed), compact=True)
        message_cost = estimate_tokens(policy_messages(state, decision)) + 768
        def input_cost(names):
            return message_cost + estimate_tokens([self.coordinator_schemas[n] for n in sorted(names)])
        decision_input = input_cost(allowed)
        if can_inspect and not budget.has_tokens(self.close_tokens + decision_input + 1):
            allowed = controls
            decision_input = input_cost(controls)
        if (not budget.has_steps(3 if self.assigned else 2) or budget.seconds_left <= self.close_seconds
                or not budget.has_tokens(self.close_tokens + decision_input + 1)):
            return closing
        dispatch_limit = decision_input + 8192
        return StepPolicy("coordinate", instruction, allowed, compact=True,
                          bound_handoff=bound,
                          token_reserve=0 if budget.tokens_left is None else max(self.close_tokens, budget.tokens_left - dispatch_limit),
                          seconds_reserve=self.close_seconds)

    def _missing_delivery_notice(self):
        missing = self.inventory["missing_acceptance_files"]
        if not missing:
            return ""
        return "宿主核对的公开工作区仍缺少这些约定交付：" + _json(missing) + "。请逐项列为未交付。\n"

    def _blocked_review_record(self, review):
        if not review or review.state != "blocked":
            return ""
        for report in reversed(self.reports):
            native = report.get("native_review") or {}
            if native.get("target") != review.target or native.get("candidate_version") != review.version:
                continue
            signed = native.get("assessment") or {}
            if (signed.get("target") != review.target or signed.get("candidate_version") != review.version
                    or signed.get("evaluator") != review.reviewer):
                return ""
            findings = signed["results"]
            passed = [row["criterion_id"] for row in findings if row["verdict"] == "passed"]
            receipts = {entry["receipt_id"] for row in findings for entry in row["receipts"]}
            blockers = [f"{row['criterion_id']}：{row.get('action') or row['reason']}"
                        for row in findings if row["verdict"] != "passed"]
            issue = review.host_issue or report.get("remaining_issue") or ""
            details = list(dict.fromkeys(x for x in [issue, *blockers] if x))
            return (f"评审者 {review.reviewer} 已对版本 {review.version[:12]} 签署 {len(findings)} 项，"
                    f"其中 {len(passed)} 项判为通过，共引用 {len(receipts)} 条不同回执。"
                    + "本次任务仍未通过验收。" + _clip("\n".join(details), 900) + "\n")
        return ""

    def published_answer(self, answer):
        if self.direct or not answer.lstrip().startswith("[BLOCKED]"):
            return answer
        missing = self.inventory["missing_acceptance_files"]
        notice = "未交付：" + "、".join(missing) + "。" if missing else ""
        if signed := self._blocked_review_record(self._next_review()):
            return "[BLOCKED] " + signed.rstrip() + ("\n" + notice if notice else "")
        if not notice:
            return answer
        return ("[BLOCKED] Studio 已核对公开工作区。" + notice + "\n\n"
                + answer.lstrip().removeprefix("[BLOCKED]").lstrip())

    def _review_requests(self, role, review=None):
        return [item for item in self.pending_check_reviews.values()
                if role not in {item["author"], item.get("last_editor")}
                and (review is None or review.kind != "reviewer_probe" or item["task"] == review.target)]

    @staticmethod
    def _review_instructions(requests):
        ids = ", ".join(r["task"] for r in requests)
        return ("前一位成员有待独立复核的测试。请对照用户需求核验修订的预期；带有 scope 的待验证检查需要在原工作目录直接完整重跑并保留退出码。"
                "如亲自核验成功，在最终交接写 [CHECKED 原任务ID] 并说明测试和依据。无法确认时说明缺口。"
                "等待核验的原任务ID：" + ids)

    def _record_check_reviews(self, role, task_id, summary, evidence, original, changes, native_target=None):
        """A revised self-check can reach a peer. The author cannot approve it."""
        reviewed = []
        changed = {entry["path"] for entry in changes}
        versions = {entry["path"]: entry.get("sha256") for entry in changes}
        for pending in self.pending_check_reviews.values():
            candidate = pending.get("candidate", {})
            touched = candidate.keys() & changed
            if touched:
                # A new published revision needs its own independent run.
                # The editor may supply a fix but cannot approve that fix.
                candidate.update({path: versions[path] for path in touched})
                pending["last_editor"] = role
                for check in pending.get("verification", []):
                    if check["path"] in touched:
                        check["current_sha256"] = versions[check["path"]]
        completed_scripts = {scope: (receipt["path"], receipt["sha256"])
                             for scope, receipt in evidence.get("script_exits", {}).items()
                             if receipt.get("exit_code") == 0}
        for item in self._review_requests(role):
            marker = "[CHECKED " + item["task"] + "]"
            scopes = item.get("verification", [])
            if ((marker in summary or item["task"] == native_target) and evidence.get("check_signals")
                    and all(c["scope"] in evidence.get("complete_verification", {})
                            or completed_scripts.get(c["scope"]) == (c["path"], c.get("current_sha256")) for c in scopes)
                    and all(original.get(path) == digest for path, digest in item.get("candidate", {}).items())
                    and all(not c["path"] or c["path"] not in changed and
                            original.get(c["path"]) == c.get("current_sha256") for c in scopes)
                    and all(original.get(f["path"]) == f["current_sha256"] and f["path"] not in changed
                            for f in item["changes"])):
                del self.pending_check_reviews[item["task"]]
                reviewed.append(item["task"])
        revised = evidence.get("superseded_member_checks", [])
        verification = evidence.get("pending_verification", [])
        if revised or verification:
            candidate = {entry["path"]: entry.get("sha256") for entry in changes} if verification else {}
            candidate.update({c["path"]: c["current_sha256"] for c in verification if c.get("path")})
            self.pending_check_reviews[task_id] = {"author": role, "task": task_id, "changes": revised,
                                                  "verification": [dict(c) for c in verification], "candidate": candidate}
        return reviewed

    def _review_answers(self):
        replies = [e.get("text", "") for e in self.room_events(self.run_id) if e.get("kind") == "human_answer"]
        return _json(replies)

    def before_answer(self):
        if self.direct and (self.native_reviews.items or self.native_reviews.required_source_paths):
            try:
                self.snapshot_direct_changes()
            except (SnapshotError, OSError) as exc:
                return "提交独立验收前无法核对候选当前版本：" + str(exc)
        if getattr(self, "invalid_route", None) and self.dispatch_ended:
            return self.dispatch_ended
        queue = getattr(self, "native_reviews", None)
        if queue and queue.items:
            pending_review = self._next_review()
            if pending_review:
                return ("独立验收尚未通过，需要 " + pending_review.owner + " 处理 " + pending_review.state +
                        ("。用 delegate_task 委派原验收者，省略 review 以沿用原标准；有新的可复核来源时告诉验收者。"
                         if pending_review.verification_gaps() else "。") + "当前目标与缺口：" + _clip(_json(pending_review.briefing()), 1900))
        published = [r for r in self.reports if r["published"]]
        if not published and not self.direct:
            return "本次群任务还没有成员完成交接。请使用 delegate_task 邀请合适成员，再汇总交付。"
        missing = set(self.thread.get("addressed_roles", [])) - {r["role"] for r in published}
        if self.direct:
            missing.discard(self.lead)
        if missing:
            return "用户点名的成员仍需完成此次分工：" + "、".join(sorted(missing)) + "。"
        pending = (((self.planned or set()) | self.assigned) - self.approved_roles - self.covered_roles
                   | (set(self.child_checks) - self.approved_roles))
        if pending:
            return "这些成员的交接尚未完成或未通过用户验收：" + "、".join(sorted(pending)) + "。请安排其解决缺口。"
        if self.pending_check_reviews:
            return self._peer_check_instruction()
        if queue and queue.required_source_paths:
            missing = queue.missing_source_reviews()
            if missing:
                candidates = {p: next((task for task, files in reversed(queue.candidates.items())
                                      if files.get(p) and files[p] == queue.public_versions.get(p)), None)
                              for p in missing}
                return ("指定成稿当前版本缺少独立来源核查（文件及可引用的候选任务）：" + _json(candidates) +
                        "。有候选时委派非作者，以 delegate_task.review 的必需 source 标准核对成稿及原始来源；"
                        "没有本轮候选时，先以成稿路径引用同群可验证的原发布；本轮新增或修订的成稿由作者交接。")
        return None

    def _observation(self, ok, text, *, bound_definitions=False):
        self.last_dispatch_error = "" if ok else text
        if not ok and not bound_definitions:
            self.last_dispatch_error = _clip(text, 1200)
        return Observation(step_id=self.run_id + ":delegate", source="delegate", ok=ok,
                           content=text, error_class=None if ok else ErrorClass.A_TOOL_ERROR)

    def _reject_destination(self, destination, message):
        decision = (destination, self.project_version, len(self.reports))
        self.invalid_route = decision
        return self._observation(False, message)

    def _split_budget(self, count, available_seconds):
        b = self.budget
        next_dispatch = min(14000, self.close_tokens + 2000)
        lead_retry = int(b.has_steps(4 * count + 1))
        reserved = Budget(
            steps_left=None if b.steps_left is None else count + lead_retry,
            actions_left=None if b.actions_left is None else count,
            tokens_left=0 if b.tokens_left is None else self.close_tokens + (count - 1) * next_dispatch,
            seconds_left=self.close_seconds + min(self.close_seconds / 2, 15) * (count - 1),
        )
        share = Budget(
            steps_left=None if b.steps_left is None else (b.steps_left - reserved.steps_left) // count,
            actions_left=None if b.actions_left is None else (b.actions_left - reserved.actions_left) // count,
            tokens_left=None if b.tokens_left is None else (b.tokens_left - reserved.tokens_left) // count,
            seconds_left=(available_seconds - reserved.seconds_left) / count,
        )
        return share, reserved

    def _allocation(self, role, planned, review=None, recovery=False, *, record_shares=True):
        child, reserved, protected = self._allocation_plan(role, planned, review, recovery)
        if record_shares:
            self.untapped_shares = protected
        return child, reserved

    def _allocation_plan(self, role, planned, review=None, recovery=False):
        available_seconds = min(self.budget.seconds_left, self.deadline - time.monotonic())
        addressed = set(self.thread.get("addressed_roles", []))
        if self.direct:
            addressed.discard(self.lead)
        required = (set(self.child_checks) | addressed) - self.approved_roles
        required.add(role)
        if review and review.state == "repair":
            # A changed candidate must return to its bound independent reviewer,
            # even when that reviewer has already published on the old version.
            required.add(review.reviewer)
        optional = set(planned or ()) - self.approved_roles - self.covered_roles - required
        participants = required if review or recovery else required | optional
        child, reserved = self._split_budget(len(participants), available_seconds)
        if participants != required and (child.exhausted or not child.has_tokens(1000)):
            participants = required
            child, reserved = self._split_budget(len(required), available_seconds)

        # A first estimate is a claim on unused room capacity, not prepaid usage.
        # Clamp every peer's claim to its share of the CURRENT available balance.
        waiting = ((participants - self.started_roles) | required) - {role}
        floors = {"steps_left": 6, "actions_left": 8, "seconds_left": 30, "tokens_left": 28000}
        finite = {field: floor for field, floor in floors.items() if getattr(child, field) is not None}
        protected = {}
        if not child.exhausted and child.has_tokens(1000):
            for peer in waiting:
                previous = self.untapped_shares.get(peer)
                claim = child.model_copy()
                for field, minimum in finite.items():
                    share = getattr(child, field)
                    estimate = getattr(previous, field) if previous else max(minimum, math.ceil(.6 * share))
                    setattr(claim, field, min(share, estimate))
                protected[peer] = claim

            entitlement = self.untapped_shares.get(role)
            if entitlement:
                for field in finite:
                    balance = available_seconds if field == "seconds_left" else getattr(self.budget, field)
                    remaining = balance - getattr(reserved, field) - sum(getattr(v, field) for v in protected.values())
                    setattr(child, field, min(max(getattr(child, field), getattr(entitlement, field)), remaining))
        return child, reserved, protected

    def _resume_interrupted_repair(self, review, frozen, progress):
        """A timed-out author's checkpoint is an execution recovery, never a product verdict."""
        author, reviewer = review.author, review.reviewer
        share, reserved, protected = self._allocation_plan(author, self.planned, review)
        peer = protected.get(reviewer)
        capacity = {role: round(allocation.seconds_left, 3) for role, allocation in
                    ((author, share), (reviewer, peer)) if allocation is not None}
        receipt = {"provider_error_code": "deadline_exceeded", "checkpoint_task": frozen["task_id"],
                   "remaining_seconds": round(max(0, min(self.budget.seconds_left, self.deadline - time.monotonic())), 3),
                   "member_seconds": capacity, "reserved_seconds": reserved.seconds_left}
        obstruction = ""
        if self.should_cancel():
            obstruction = "用户已取消本轮。"
        elif not frozen["checkpoint"] or not frozen["manifest"]:
            obstruction = "成员请求到期，未取得可安全恢复的冻结草稿。"
        elif peer is None or any(allocation.exhausted or not allocation.has_steps(2)
                or not allocation.has_actions(2) or not allocation.has_tokens(1000)
                for role, allocation in ((author, share), (reviewer, peer))):
            obstruction = "成员请求到期，剩余额度不足以安排原作者返工、原验收者重新取证签署和负责人收尾。"
        else:
            obstruction = review.interrupted_repair(frozen, progress.review_evidence())
        if obstruction:
            review.state = "blocked"
            review.host_issue = obstruction
        else:
            # Preserve the original signed version. Only the original author
            # may restore this newer, still-unpublished working checkpoint.
            frozen["repair_for"] = (review.target, review.version)
        return {**receipt, "resume": not obstruction, "reason": obstruction or "按冻结检查点恢复原作者分工，保留原验收者。"}

    def _eligible_repairs(self, origin):
        elected = list(dict.fromkeys([*self.plan_order, *self.child_checks]))
        eligible = []
        for role in elected:
            if role == origin or (self.project_version, origin, role) in self.routed_repairs:
                continue
            allocation, _ = self._allocation(role, self.planned, recovery=True, record_shares=False)
            if not allocation.exhausted and allocation.has_tokens(1000):
                eligible.append(role)
        return sorted(eligible, key=lambda r: r in self.started_roles)

    def _decline_retry(self, role, reason, payload):
        draft = self.private_drafts.get(role, {})
        paths = draft.get("required") or payload.get("deliverable_paths") or []
        eligible = [peer for peer in self._eligible_repairs(role) if peer != self.lead or paths]
        if not eligible or self.should_cancel():
            self.dispatch_ended = reason
            return self._observation(False, reason)
        if role not in self.refused_retry:
            self.refused_retry.add(role)
            return self._observation(False, reason + " 可接手的已选成员：" + "、".join(eligible))
        # The coordinator has asked for the same exhausted role again despite
        # the denial. Route this specific repair within its own elected plan.
        recipient = eligible[0]
        self.routed_repairs.add((self.project_version, role, recipient))
        recoverable = draft.get("version") == self.project_version and bool(draft.get("checkpoint"))
        issue = _clip(draft.get("issue") or reason, 1200)
        objective = ("按你的角色职责接手并修复上一阶段的具体阻塞。核实原始任务要求和证据，"
                     "解决问题后实际运行对应检查，报告结果与剩余限制。\n原成员：" + role +
                     "\n待修复问题：" + issue + "\n你在团队中的职责：" + self.profiles[recipient]["prompt"])
        retry = {"assignee": recipient, "objective": objective,
                 "acceptance": "提供实际修复及针对上述问题的复核证据，保留原有可用文件和数据。",
                 "__runtime_action_id": payload["__runtime_action_id"]}
        if paths:
            retry["deliverable_paths"] = paths
        if recoverable:
            retry["recover_from"] = role
        self.record_event(self.run_id, "studio", "runtime_reroute",
                          "原成员的重试已受限，运行时按负责人已选的分工顺序转交。", requested=role,
                          recipient=recipient, reason=reason, recover_from=role if recoverable else None,
                          task_id=self.run_id)
        return self.delegate(retry, routed_from=role)

    def _remember_draft(self, role, assignment, required, report, failed_checks, private_receipts, original, resumed=None, inline_failures=None, *, evidence_only=False):
        checkpoint = assignment / "checkpoint"
        directory = checkpoint / "agents" / role
        try:
            copy_project(assignment, role, directory, self.extensions, secret=self.settings.api_key)
            manifest = _manifest(directory)
        except (OSError, SnapshotError):
            checkpoint, manifest = None, {}
        evidence = report["evidence"]
        missing = sorted(set(required) - set(manifest))
        # copy_project already validated every manifest entry. Include every
        # source/config format it supports. Incidental logs and databases
        # can change on every smoke test; count them when explicitly assigned.
        tracked = sorted(p for p in manifest.keys() | original.keys()
                         if p in required or Path(p).suffix.lower() not in _INCIDENTAL_DATA)
        source_hashes = [(p, manifest.get(p)) for p in tracked]
        signature = hashlib.sha256(_json([missing, source_hashes, sorted(failed_checks)]).encode()).hexdigest()
        substantive = bool(failed_checks) or any(original.get(p) != h for p, h in source_hashes)
        previous = self.private_drafts.get(role, {})
        signatures = dict(previous["signatures"]) if previous.get("version") == self.project_version else {}
        signatures[signature] = signatures.get(signature, 0) + 1
        reviewable = bool(required and not missing and checkpoint and manifest and manifest != original)
        # The signature still detects byte-identical retries. For a complete
        # candidate, also compare attributable observations across a real
        # repair of the same private lineage. New wording is not a closed check.
        unresolved = bool(reviewable and previous is resumed and previous.get("reviewable") and
                          previous.get("version") == self.project_version and
                          previous["failed_checks"] and previous["failed_checks"].keys() <= failed_checks.keys())
        origin = (resumed or {}).get("origin") or {"task":report["task_id"], "objective":report["objective"],
                                               "acceptance":report.get("acceptance_expectation", "")}
        probes = {}
        available = {**(resumed or {}).get("inline_failures", {}), **(inline_failures or {})}
        total = 0
        for key in failed_checks:
            if key not in available:
                continue
            probe = available[key]
            data = (probe["code"] + "\n" + probe["error"]).encode()
            if total + len(data) > 98304:
                continue
            try:
                validate_project_data("probe.txt", data, secret=self.settings.api_key, reject_credentials=True)
            except ValueError:
                continue
            probes[key] = probe
            total += len(data)
        undisclosed = sorted(set(available).intersection(failed_checks).difference(probes))
        if undisclosed:
            # No missing source is silently treated as reviewed. These scopes
            # stay in failed_checks and are restored as mandatory blockers.
            evidence["untransferred_failure_probes"] = undisclosed
        if reviewable and not evidence_only:
            report["reviewable_candidate"] = {"task_id": report["task_id"], "candidate_version": digest(manifest),
                                            "files_total": len(manifest)}
            if unresolved:
                report["reviewable_candidate"]["unresolved_after_repair"] = sorted(previous["failed_checks"])
        frozen = {
            "task_id": report["task_id"], "manifest": manifest, "base": dict(original), "reviewable": reviewable,
            "review_after_repair": unresolved, "inline_failures": probes, "origin": origin,
            "version": self.project_version, "checkpoint": checkpoint, "required": required,
            "missing": missing, "role": role, "issue": _clip(report["remaining_issue"], 1000),
            "evidence": evidence, "failed_checks": failed_checks, "receipts": private_receipts,
            "signatures": signatures, "repeated_state": signatures[signature] > 1,
            "input_revision": signature if substantive and checkpoint else None,
        }

        if not evidence_only:
            self.private_drafts[role] = frozen
        return frozen

    def delegate(self, payload, *, routed_from=None):
        role = payload.get("assignee")
        objective = payload.get("objective")
        action = payload.get("__runtime_action_id")
        if not isinstance(role, str) or role not in self.delegate_recipients:
            hint = "请明确指定本组可执行成员。assignee 可填：" + _json(self.delegate_recipients) + "。"
            return self._reject_destination(("recipient", role), hint)
        if self.read_only and (payload.get("deliverable_paths") or payload.get("review") or payload.get("recover_from")):
            return self._observation(False, "本轮为只读讨论。请邀请成员查阅资料并在群内回复，省略文件交付、修改和正式评审。")
        if not isinstance(objective, str) or not objective.strip() or len(objective) > 10000 or not action:
            return self._observation(False, "委派需要具体目标和运行时动作身份。")
        if action in self.handoffs:
            return self.handoffs[action]
        if self.direct:
            try:
                self.snapshot_direct_changes()
            except (SnapshotError, OSError) as exc:
                return self._observation(False, "向协作者提供项目版本失败：" + str(exc))
        review = self._next_review()
        if role == self.lead:
            # A bound repair or review already supplies the exact input files
            # and standards; an adjudicator can sign without inventing a file.
            bound_work = review and review.state in {"repair", "review"} and review.owner == role and review.files
            if not (payload.get("review") or bound_work or payload.get("deliverable_paths") or self.read_only
                    or role in self.thread.get("addressed_roles", [])):
                peers = [name for name in self.profiles if name != self.lead]
                hint = ("请在 deliverable_paths 列出负责人承担的具体文件；独立验收可交给其他成员："
                        + _json(peers) + "。")
                return self._reject_destination(("lead_assignment", role), hint)
        if review and review.kind == "reviewer_probe" and payload.get("review") is not None:
            return self._observation(False, "当前须裁决已绑定的评审探针。请省略 review，沿用宿主逐项标准。" +
                                     self._probe_dispatch_hint(self._pending_probe()))
        peer_check = False
        if self._only_peer_checks_remaining():
            selection = payload.get("review")
            # A rejected reopening of an already-passed product may be routed
            # to its remaining test obligation. A different candidate keeps its
            # own review request and criteria; never silently consume it here.
            candidate = selection.get("candidate") if isinstance(selection, dict) else None
            passed = self._passed_review_receipts()
            matches = [r for r in passed if isinstance(candidate, str)
                       and (candidate == r["target"] or candidate in r["files"])]
            try:
                requested = ReviewRequest.model_validate(selection)
                same_rules = len(matches) == 1 and sorted(
                    (c.model_dump(exclude_none=True) for c in requested.criteria), key=lambda c: c["id"]) == sorted(
                    (c.model_dump(exclude_none=True) for c in self.native_reviews.items[matches[0]["target"]].criteria), key=lambda c: c["id"])
            except ValueError:
                same_rules = False
            # Ordinary assignments remain ordinary. An author alias can refer
            # to another product. New criteria must not be silently discarded.
            if same_rules:
                requests = self._pending_check_briefing()
                actionable = [r for r in requests if r["eligible_roles"]]
                item = next((r for r in actionable if role in r["eligible_roles"]), actionable[0] if actionable else requests[0])
                peers = item["eligible_roles"]
                if not peers:
                    return self._observation(False, "待复核检查没有可独立核销的其他成员。" + self._peer_check_instruction())
                recipient = role if role in peers else next((p for p in self.plan_order if p in peers), peers[0])
                selected = [r for r in requests if recipient in r["eligible_roles"]]
                objective = self._peer_check_instruction(selected, for_member=True)
                payload = {k: v for k, v in payload.items() if k not in {"review", "recover_from"}}
                payload.update(assignee=recipient, objective=objective, deliverable_paths=[], acceptance="")
                self.record_event(self.run_id, "studio", "peer_check_routed", "产品评审已通过，转交剩余检查供另一成员独立复核。",
                                  task_id=self.run_id, requested=role, recipient=recipient,
                                  pending_tasks=[r["task"] for r in selected], product_reviews=matches)
                routed_from, role, peer_check = role, recipient, True
        if not peer_check and review is None and payload.get("review") is None:
            donor = payload.get("recover_from", role)
            draft = self.private_drafts.get(donor) if isinstance(donor, str) else None
            if draft and draft.get("review_after_repair") and draft["version"] == self.project_version:
                peers = [peer for peer in self._eligible_repairs(donor) if peer != self.lead]
                if not peers:
                    return self._decline_retry(donor, "相同失败项在续修后仍未关闭，当前没有可独立验收的已选成员。", payload)
                # The coordinator sees the roster responsibilities and can name
                # its evaluator. If it again selects the author, use a previous
                # reviewer from the elected plan, then the plan's peer order.
                prior_reviewers = {r.reviewer for r in self.native_reviews.items.values()}
                prior_reviewers.update(r["role"] for r in self.reports if r.get("independently_reviewed_tasks"))
                recipient = role if role in peers else next((p for p in peers if p in prior_reviewers), peers[0])
                expected = draft["origin"]["acceptance"] or draft["origin"]["objective"]
                standard = {"id": "independent_recheck", "method": "execute",
                            "description": "请对照原分工与用户要求，在冻结候选上独立验证未关闭的失败项。"
                                           + _clip(expected, 1050)}
                selection = {"candidate": draft["task_id"], "criteria": [standard]}
                # Author deliverables are already frozen inputs, never peer outputs.
                payload = {**payload, "assignee": recipient, "review": selection, "deliverable_paths": []}
                self.record_event(self.run_id, "studio", "runtime_reroute", "原失败项在续修后仍未关闭，转交冻结候选供独立验收。",
                                  requested=role, recipient=recipient, candidate_task=draft["task_id"],
                                  candidate_version=digest(draft["manifest"]), task_id=self.run_id)
                routed_from, role = donor, recipient
        if payload.get("review") is not None:
            try:
                candidates = ([self.direct_candidate] if self.direct_candidate else []) + self.reports
                requested_review = self.native_reviews.open(payload["review"], role, candidates,
                    drafts=self.private_drafts, project_version=self.project_version, inherited=self.inherited_review)
            except CandidateLookupError as exc:
                known = list(self.native_reviews.path_candidates)[:8]
                message = ("无法发起独立验收：" + str(exc) + " 本轮已发布的候选路径：" + _json(known)
                           + "。如果材料只是尚无本群发布记录的输入，省略 review，给独立成员分配读取原文并记录意见的任务。")
                return self._reject_destination(("candidate", role, payload["review"]["candidate"]), message)
            except (ValueError, OSError) as exc:
                remaining = "\n" + self._peer_check_instruction() if self._only_peer_checks_remaining() else ""
                return self._observation(False, "无法发起独立验收：" + str(exc) + remaining,
                                         bound_definitions=isinstance(exc, InheritedCriteriaConflict))
            if review and review is not requested_review:
                return self._observation(False, "请先完成已在进行的独立验收。")
            review = requested_review
        retry_gaps = review.verification_gaps() if review else []
        original_recipient = role
        if retry_gaps and role != review.owner:
            # The other member's new report is a separate assignment. Finish
            # the bound review before adding work for a different reviewer.
            routed_from = role
            payload = {**payload, "deliverable_paths": [], "acceptance": ""}
        if review:
            if review.state in {"passed", "blocked", "awaiting_user"}:
                return self._observation(False, "该验收当前状态：" + review.state)
            role = review.owner
            review_instruction = "对同一目标的当前候选执行独立验收，使用 submit_review 提交逐项判断。"
            if review.state == "repair":
                review_instruction = "针对原目标返工，处理独立评估发现的产品缺口。"
                changes = review.required_product_changes(review.latest)
                if any(rule.method == "source" and rule.id in changes for rule in review.criteria):
                    review_instruction += (
                        "\n来源退修：评审建议本身不能作为事实依据。用 http_get 打开原始发布者的具体材料，"
                        "逐条将争议断言与原文、表头或脚注对照。有数字时说明对象、期间、单位和分母；"
                        "涉及正负号时区分原项目的贡献和为剔除它所作的调整。"
                        "原文支持修改就更正；原文支持现稿则保留已核实的事实，在新版补上可定位的引文、"
                        "表格行或计算依据及原始链接，并在交接里解释分歧。证据不足就明确标记待核实。"
                        "由原评审者按同一标准独立核查新版并重新签署。")
            objective = (review_instruction +
                         ("\n本次复核线索（仅供核对，验收标准沿用原件）：" + _clip(objective, 1600) if retry_gaps else "") +
                         "\n原目标及标准：" + _clip(_json(review.briefing()), 7000))
        plan = self.planned
        if "remaining_roles" in payload:
            rest = payload["remaining_roles"]
            if (not isinstance(rest, list) or len(rest) > len(self.profiles)
                    or any(not isinstance(r, str) or r not in self.delegate_recipients for r in rest)):
                return self._observation(False, "remaining_roles 应只列出本群后续需要的成员 ID。")
            plan = (plan or set()) | {role, *rest}
            for named in [role, *rest]:
                if named not in self.plan_order:
                    self.plan_order.append(named)
        requested_paths = payload.get("deliverable_paths", [])
        if review and review.state == "repair" and not review.draft_task:
            requested_paths = [p for p,v in review.files.items() if v is not None]
        if (not isinstance(requested_paths, list) or len(requested_paths) > 32
                or any(not isinstance(path, str) or len(path) > 256 for path in requested_paths)):
            return self._observation(False, "deliverable_paths 需要最多 32 个项目内的相对文件名。")
        try:
            paths = sorted({_relative_name(path) for path in requested_paths})
        except ValueError:
            return self._observation(False, "deliverable_paths 中有无效的相对路径。")
        if review and review.state == "review" and review.revision_reports:
            paths = sorted(set(paths) | review.revision_reports)
        for path in paths:
            if (not supported_project_file(path, self.extensions) or any(s.startswith(".") for s in path.split("/"))
                    or any(s in PROJECT_CACHE_DIRS for s in Path(path).parts[:-1])):
                return self._observation(False, "deliverable_paths 中的文件无法公开交接"
                                         + snapshot_location(Path(path), self.settings.api_key)
                                         + "。如属用户指定原件，请保留并报告交付格式限制。")
        expectations = str(payload.get("acceptance") or "")[:2000]
        user_state = self._clarifications(self.room_events(self.run_id))
        donor = (review.author if review.draft_task else role) if review else payload.get("recover_from", role)
        if not isinstance(donor, str) or donor not in self.delegate_recipients:
            return self._observation(False, "recover_from 只能选择本群已有受阻交接的成员。")
        arbitration = self.reviewer_probe_reviews.get(review.target, {}) if review else {}
        draft = arbitration.get("private_evidence") or self.private_drafts.get(donor)
        if peer_check:
            # This is a review of the current published snapshot, never a
            # continuation of the recipient's older private implementation.
            draft = None
        if review and not review.draft_task and review.state == "review":
            draft = None
        continuing_repair = (review and review.state == "repair" and draft and role == review.author
                             and draft.get("repair_for") == (review.target, review.version))
        if review and review.draft_task and (not draft or draft["version"] != self.project_version
                or not continuing_repair and (draft["task_id"] != review.draft_task or draft["manifest"] != review.files)):
            review.state = "blocked"
            review.host_issue = ("候选检查点或公共版本已变化，此次冻结裁决已失效。请保留已有证据，"
                                 "在同群下一轮选择已发布的当前版本发起独立复核。")
            if self.direct and draft and (difference := manifest_issue(draft["base"], self.direct_baseline,
                                                                      exact=True, secret=self.settings.api_key)):
                review.host_issue += "公开基线差异：" + difference
            return self._observation(False, review.host_issue)
        if draft and draft["version"] != self.project_version:
            draft = None
        transfer = donor != role
        if transfer and (not draft or not draft["checkpoint"]):
            return self._observation(False, "没有通过宿主快照校验的当前版本私有草稿。请使用公共版本。")
        if not review and not transfer and draft and draft["repeated_state"]:
            return self._decline_retry(role, "同一公开版本上的分工已重试，文件缺口和草稿未取得足够进展。"
                                       "请点名其他合适成员，用 recover_from 接手草稿，或如实说明阻塞。", payload)
        if draft:
            # Renaming the task cannot drop an unfinished delivery obligation.
            paths = sorted(set(paths) | set(draft["required"]))
        identity = (role, objective.strip(), expectations.strip(), tuple(paths), user_state, donor,
                    (review.target, review.version, review.state, len(review.visited)) if review else None)
        self.planned = plan
        # The arbiter owns a signature in a pending review, not an assignment
        # to publish files. Keep any unrelated delivery obligations intact.
        private_probe = bool(arbitration.get("private_product"))
        if not private_probe:
            self.assigned.add(role)
        prior = self.assignment_results.get(identity)
        input_revision = draft["input_revision"] if draft else None
        if prior and prior[0] == self.project_version and (prior[1].ok or prior[2] == input_revision):
            detail = "已成功交接" if prior[1].ok else "已受阻：" + _clip(prior[1].content, 500)
            message = "这项分工在当前版本" + detail + "。请根据报告指定具体修复、安排下一项分工或汇总。"
            if not prior[1].ok:
                return self._decline_retry(role, message, payload)
            return self._observation(False, message)
        if not private_probe:
            self.approved_roles.discard(role)
            self.covered_roles.discard(role)
        allocated, reserved = self._allocation(role, plan, review, peer_check or draft is not None)
        if self.should_cancel():
            self.dispatch_ended = "本轮已取消。"
            return self._observation(False, self.dispatch_ended)
        if allocated.exhausted or not allocated.has_tokens(1000):
            remaining = self.budget.model_dump()
            remaining["seconds_left"] = max(0, min(remaining["seconds_left"], self.deadline - time.monotonic()))
            self.dispatch_ended = "已无委派预算。剩余额度无法同时安排这项分工和负责人收尾。"
            self.record_event(self.run_id, "studio", "dispatch_budget_exhausted", self.dispatch_ended,
                              task_id=self.run_id, recipient=role, remaining=remaining, reserved=reserved.model_dump(),
                              allocation=allocated.model_dump(), review_state=review.state if review else None)
            return self._observation(False, self.dispatch_ended)
        self.routing_denied = False
        self.invalid_route = None
        self.started_roles.add(role)
        dispatch_started = time.monotonic()
        if review:
            if review.publication and review.target not in self.native_reviews.items:
                self.record_event(self.run_id, "studio", "published_review_bound", "已绑定同群先前发布的原件，开始本轮独立复核。",
                                  task_id=self.run_id, reviewer=role, publication=review.publication,
                                  candidate_version=review.version, criteria=[r.model_dump(exclude_none=True) for r in review.criteria])
            if review.kind != "reviewer_probe":
                self.native_reviews.items[review.target] = review
        if retry_gaps:
            self.record_event(self.run_id, "studio", "review_resumed", "原标准继续生效，已将未决项交给原验收者。",
                              task_id=self.run_id, requested=original_recipient, recipient=role, candidate_task=review.target,
                              candidate_version=review.version, pending_criteria=[g["criterion_id"] for g in retry_gaps],
                              bound_criteria=[r.model_dump(exclude_none=True) for r in review.criteria])
        try:
            outcome = self._dispatch(role, objective, expectations, action, allocated, reserved, paths, draft, routed_from, review)
        finally:
            self.nested_seconds += time.monotonic() - dispatch_started
        self.assignment_results[identity] = (self.project_version, outcome, input_revision)
        self.handoffs[action] = outcome
        return outcome

    def _editable_review_reports(self, role, required, original, review):
        """Authorize only this reviewer's explicitly assigned, byte-matched earlier reports."""
        editable = set()
        current = {r["task_id"]: r for r in self.reports if r["published"]}
        history = {turn["run_id"] for turn in self.history}
        for path in required:
            if (path not in original or path in review.files or path in self.native_reviews.required_source_paths
                    or Path(path).suffix.lower() not in {".md", ".rst", ".txt"}):
                continue
            binding = self.native_reviews.path_candidates.get(path)
            report = None
            if binding:
                task, sha = binding
                if sha == original[path]:
                    report = current.get(task)
            elif self.inherited_review and history:
                try:
                    publication = self.inherited_review(path)
                except ValueError:
                    continue
                if (publication and publication["run_id"] in history and publication["role"] == role
                        and publication["files"].get(path) == original[path]):
                    report = next((e["handoff"] for e in self.room_events(publication["run_id"])
                                   if e["kind"] == "member_report" and e.get("published") and e["role"] == role
                                   and e.get("task_id") == publication["task_id"]), None)
            if report and report["role"] == role and path in report["deliverable_paths"]:
                editable.add(path)
        return editable

    def _dispatch(self, role, objective, expectations, action, allocated, reserved, paths, draft, routed_from, review=None):
        assignments = self.path / "assignments"
        assignments.mkdir(exist_ok=True)
        assignment = Path(tempfile.mkdtemp(dir=assignments, prefix="member-"))
        workspace = assignment / "agents" / role
        try:
            copy_project(self.path, self.lead, workspace, self.extensions, secret=self.settings.api_key)
            original = _manifest(workspace)
        except (SnapshotError, OSError) as exc:
            return self._observation(False, f"项目文件无法交给成员：{exc}")
        recovered = False
        if draft and draft["checkpoint"]:
            restored = assignment / "restored"
            try:
                copy_project(draft["checkpoint"], draft["role"], restored, self.extensions, secret=self.settings.api_key)
                if review and (review.draft_task or draft.get("repair_for")):
                    if original != draft["base"] or _manifest(restored) != draft["manifest"]:
                        review.state = "blocked"
                        raise SnapshotError("冻结草稿或公共基线已有变化。")
            except (SnapshotError, OSError) as exc:
                if review and review.draft_task:
                    review.state = "blocked"
                return self._observation(False, f"私有草稿未通过快照校验：{exc}")
            else:
                workspace.rename(assignment / "public-base")
                restored.rename(workspace)
                recovered = True
        parent = self.service.store.get(self.run_id)
        record = self.service.create(parent_id=self.run_id, creation_key=f"delegation:{self.run_id}:{action}",
                                     objective=objective, owner=self.lead, assignee=role,
                                     project_id=parent.project_id)
        self.record_event(self.run_id, "studio" if routed_from else self.lead, "assignment", objective,
                          recipient=role, task_id=record.task_id, routed_from=routed_from,
                          expectation=expectations, allocation=allocated.model_dump(), reserved=reserved.model_dump(),
                          planned=sorted(self.planned) if self.planned is not None else None,
                          deliverable_paths=paths, resumed_private_draft=recovered,
                          recover_from=draft["role"] if recovered else None,
                          protected_peer_shares={k:v.model_dump() for k,v in self.untapped_shares.items()})
        execution = self.service.start(record.task_id, expected_revision=record.revision,
                                       context={"role": role, "workspace": str(workspace), "public_room": self.thread["id"]})
        handoff_seconds = min(30, allocated.seconds_left * .1)
        human_deadline = time.monotonic() + allocated.seconds_left - handoff_seconds
        registry = build_default_registry(read_only=self.read_only)
        if self.diagnostics.files:
            registry.add(self.diagnostics.tool())
        if self.role_session:
            registry = self.role_session.registry(role, registry, read_only=self.read_only)
        spec = self.child_checks.get(role, {})
        required = sorted(set(spec.get("files", [])) | set(paths))
        private = bool(review and review.draft_task)
        arbitration = self.reviewer_probe_reviews.get(review.target, {}) if review else {}
        private_arbitration = bool(arbitration.get("private_product"))
        inspecting = bool(review and review.state == "review")
        # A reviewer checks the frozen author tree independently; inherited
        # artifacts count as inputs, not as writes made by the reviewer.
        baseline = review.files if private and inspecting else original
        review_notes = {}
        if private and inspecting:
            # The selected parent may already contain a previous round's QA
            # report. Leave that version at its original path during every probe.
            directory = "review-notes"
            while any(p == directory or p.startswith(directory + "/") for p in baseline):
                directory += "-new"
            for path in required:
                if path not in draft["required"] and Path(path).suffix.lower() in {".md", ".rst", ".txt"}:
                    if path not in baseline:
                        review_notes[path] = path
                    elif baseline[path] == original.get(path):
                        review_notes[path] = directory + "/" + path
            required = sorted(review_notes.get(p, p) for p in required)
        progress = MemberProgress(workspace, required_paths=required, public_baseline=baseline)
        progress.register_browser_replay(registry)
        session = None
        if inspecting:
            def private_snapshot_issue():
                if private_arbitration and (issue := self._private_arbitration_issue(arbitration)):
                    return issue
                manifest = {}
                try:
                    copy_project(assignment, role, None, self.extensions, secret=self.settings.api_key, manifest=manifest)
                except SnapshotError as exc:
                    return str(exc)
                additional_evidence = set(review_notes.values())
                if session:
                    additional_evidence.update(p for p, sha in session.source_files.items()
                                               if p not in review.files and manifest.get(p) == sha)
                products = {p: v for p, v in manifest.items() if p not in additional_evidence}
                return manifest_issue(review.files, products, exact=True, secret=self.settings.api_key)

            def promotion_issue():
                evidence = progress.evidence()
                exits = evidence.get("script_exits", {})
                for entry in evidence["pending_verification"]:
                    if entry["scope"] in evidence["complete_verification"]:
                        continue
                    receipt = exits.get(entry["scope"], {})
                    path = entry.get("path")
                    if not (path and receipt.get("path") == path and receipt.get("exit_code") == 0
                            and receipt.get("tool_call_id") and receipt.get("sha256") == review.files.get(path)
                            and (evidence["check_signals"] or self.delivery.get("checks")
                                 or self.child_checks.get(review.author, {}).get("checks"))):
                        return "私有候选的指定检查尚未在当前版本独立完成：" + entry["check"]
                failed = progress.failed_checks()
                if review.kind == "product":
                    # submit_review separately requires the current reviewer's own
                    # transferable failures to be explicitly bound to a finding.
                    local = {scope for scope, probe in progress.inline_failures().items()
                             if probe["task_id"] == record.task_id and scope not in draft["failed_checks"]}
                    failed = {scope:value for scope,value in failed.items() if scope not in local}
                if failed:
                    message = "当前版本仍有失败检查：" + "、".join(failed.values())
                    if review.kind == "reviewer_probe":
                        receipts = {r["scope"]: r["ref"] for r in session.receipts.values() if r["kind"] == "failed_check"}
                        local = [{"scope": scope, "ref": receipts.get(scope), "tool": p["tool"],
                                  "tool_call_id": p["tool_call_id"]} for scope, p in progress.inline_failures().items()
                                 if p["task_id"] == record.task_id and p["tool"] in {"run_python", "run_browser"}
                                 and scope not in draft["failed_checks"]]
                        if local:
                            message += ("。本次裁决者自己的临时检查：" + _json(local) +
                                "。若检查方法有误，请将受影响的现有标准签为 inconclusive，cause=verification，"
                                "引用实际失败及纠正证据，在 reason 和 action 写明误差与复验方法，其余项照实提交。"
                                "负责人可让同一位裁决者从原冻结版本重新取证；本次失败会留在历史交接中。")
                    return message
                if draft["evidence"].get("superseded_member_checks") and not evidence["check_signals"]:
                    return "作者修订过失败检查，请独立执行实际断言以确认改动。"
                return None

            editable_reports = set() if private else self._editable_review_reports(role, required, original, review)
            fresh_reports = {p for p in required if p not in original and Path(p).suffix.lower() in {".md", ".txt", ".rst"}}
            session = ReviewSession(review, baseline, progress, execution.attempt.attempt_id, editable_reports=editable_reports,
                                    reviewer_task=record.task_id if review.kind == "product" else None,
                                    report_outputs=fresh_reports if not private else (),
                                    available_tools={spec.name for spec in registry.specs()},
                                    private_snapshot_issue=private_snapshot_issue if private else None,
                                    promotion_issue=promotion_issue if private else None, review_notes=review_notes.values())
            session.guard_file_tools(registry)
            registry.add(session.tool())
            registry.add(session.catalog_tool())
        requested_peer = False

        def member_question(payload):
            nonlocal requested_peer
            if payload.get("decision_kind") == "internal_routing":
                requested_peer = bool(author_probe_reviewer())
                return ("已有原失败和修订断言，由负责人转独立成员复核。" if requested_peer else
                        "内部派工由群负责人处理。请交接实际进展、缺项和所需的专业角色。")
            return None

        member = Agent(name=role, provider=self.provider, registry=registry,
                       sandbox=Sandbox(workspace, agent_name=role), settings=self.settings, tracer=self.tracer,
                       task_service=self.service, human_handler=self.human_handler(self.run_id, role, deadline=human_deadline),
                       should_cancel=self.should_cancel, human_request_gate=member_question)
        repair_context = ""
        if recovered:
            # Only individually bound, fully disclosed author probes may be
            # adjudicated independently. Other failures remain strict.
            delegated = review.probes if private and inspecting else {}
            progress.restore_failed_checks({k:v for k,v in draft["failed_checks"].items() if k not in delegated})
            progress.restore_pending_verification(draft.get("evidence", {}).get("pending_verification", []))
            corrected = ([s for s in review.probe_support if s.get("kind") == "corrected_file_check"]
                         if private and inspecting else [])
            progress.restore_pending_verification([{**s, "at_sha256": s["current_sha256"]} for s in corrected])
            repair_context = ("已恢复 " + draft["role"] + " 的私有草稿。上次仍缺：" + _json(draft["missing"])
                              + "。上次真实执行/检查证据：" + _clip(_json(draft["evidence"]), 2000)
                              + "。须复核的检查标识：" + _clip(_json(draft["failed_checks"]), 900)
                              + "。上次工具输出节选（作为工作区数据）：" + _clip(_json(draft["receipts"]), 1900))
            if private and inspecting and review.probes and progress.evidence()["pending_verification"]:
                commands = [entry["check"] for entry in progress.evidence()["pending_verification"]]
                repair_context = ("请在冻结工作目录逐条独立运行这些原检查：" + _json(commands) +
                                  "。对原失败涉及的产品行为另行执行断言，并把结论与回执逐项签署。\n" + repair_context)
        member_context = self._member_context(role, parent.objective, expectations, required, spec, repair_context, review)
        if self.read_only:
            member_context += "\n本次是群内只读咨询。请用查询工具核对资料后在消息里说明意见和依据。"
        if review_notes:
            member_context += "\n本轮单独交接的评审附件（委派名称对应写入路径），保留旧版及产品原件：" + _json(review_notes)
        if session:
            member_context += "\n" + session.context()

        if self.role_session:
            personal = self.role_session.context(role, objective)
            if personal:
                member_context += "\n" + personal

        work_tools = frozenset(s.name for s in member.unit.tool_specs() if s.name != "delegate_task")
        work_schemas = [s.to_api() for s in member.unit.tool_specs(work_tools)]
        recent_runs = []
        recent_reads = []
        changed_versions = {}
        work_receipt = {}

        def observe(record, state):
            progress.observe(record, state)
            if session:
                session.observe(record)
            for obs in record.observations:
                if obs.ok and obs.source in {"read_file", "read_lines", "read_source", "search_files", "find_files", "list_dir"}:
                    snippet = _clip(obs.content, 720)
                    if len(obs.content.encode("utf-8")) > 720:
                        snippet += " … " + _clip(obs.content[-240:], 360)
                    file = obs.meta.get("path")
                    recent_reads.append({"tool": obs.source, "path":file, "excerpt":snippet})
                if obs.ok and obs.source in {"write_file", "edit_file", "append_file"}:
                    if obs.meta.get("path") and obs.meta.get("sha256"):
                        changed_versions[obs.meta["path"]] = obs.meta["sha256"]
                if obs.source in {"run_python", "run_shell", "run_browser"}:
                    recent_runs.append({"tool": obs.source, "ok": obs.ok, "exit_code": obs.meta.get("exit_code"),
                                        "output_tail": _clip(obs.content[-850:], 800)})
            del recent_reads[:-4]
            del recent_runs[:-3]
            if record.observations:
                evidence = progress.evidence()
                work_receipt.update(step=record.step, files=evidence["files"][:12], removed=evidence["removed_files"][:6],
                                    versions=dict(list(changed_versions.items())[-6:]), reads=progress.read_checkpoint(),
                                    checks=evidence["check_signals"], failed=evidence["observed_failure"],
                                    replayable_browser_call_ids=evidence["replayable_browser_call_ids"],
                                    script_exits=dict(list(evidence["script_exits"].items())[-4:]),
                                    superseded_member_checks=evidence.get("superseded_member_checks", []),
                                    diagnostic_checks=evidence["diagnostic_checks"], pending_verification=evidence["pending_verification"],
                                    failed_check_details=evidence["failed_check_details"],
                                    runs=[{k:v for k,v in run.items() if k != "output_tail"} for run in recent_runs])
                if self.tracer:
                    self.tracer.event("member_work_checkpoint", root_id=self.run_id, parent_task_id=self.run_id,
                                      task_id=record_id, attempt_id=execution.attempt.attempt_id,
                                      run_id=execution.run.run_id, level="subtask", step=record.step,
                                      checkpoint=work_receipt.copy())
        record_id = record.task_id
        handoff_limit = 6000 if allocated.tokens_left is None else min(6000, max(800, allocated.tokens_left // 48))
        group_decisions = self._handoff_decisions(min(1400, handoff_limit // 3))

        closure_replay_offered = set()
        peer_checks = ClosingPeerChecks(registry, workspace, progress.project_versions)

        def signed_verification_pending():
            if not session or review.kind != "reviewer_probe" or not session.assessment:
                return False
            gaps = review.verification_gaps(session.assessment)
            if (not gaps or self.should_cancel() or member.sandbox.command_sessions.pending_reason()
                    or progress.pending_paths() or progress.evidence()["pending_verification"] or session.snapshot_issue()):
                return False
            # Only the very measurements cited in the unresolved signed findings
            # belong to this handoff. A failure from another call is independent.
            cited = {(r["scope"], r["tool_call_id"]) for finding in gaps for r in finding["receipts"]
                     if r["kind"] == "failed_check" and r.get("tool_call_id")}
            active = progress.review_evidence()
            return all((scope, active.get("fail:" + scope, {}).get("tool_call_id")) in cited
                       for scope in progress.failed_checks())

        def member_policy(state, budget, *, preflight=False):
            peer_checks.restore_shell()
            bare = StepPolicy("handoff", allowed_tools=frozenset(), compact=True)
            reserve = 0 if budget.tokens_left is None else estimate_tokens(policy_messages(state, bare)) + handoff_limit + 1344
            # MemberProgress already holds one turn for the first handoff.
            # Keep one more for a protocol correction without spending more
            # than the allocation or taking another member's share.
            spendable = budget.model_copy(update={"steps_left": None if budget.steps_left is None else budget.steps_left - 1,
                                                  "tokens_left": budget.available_tokens(reserve),
                                                  "seconds_left": budget.seconds_left - handoff_seconds})
            if preflight:
                progress.require_handoff("工作调用的受保护输入超过可用 token；使用保留的最终交接额度")
            phase = progress.phase(spendable, state.scratch.get("delivery_feedback", ""))
            pending_command = member.sandbox.command_sessions.pending_reason()
            wants_handoff = phase == "handoff" or state.scratch.get("delivery_feedback") == pending_command
            if pending_command and wants_handoff and not preflight and spendable.has_steps(2) and not spendable.exhausted:
                return StepPolicy("work", pending_command, frozenset({"poll_process"}),
                                  token_reserve=reserve, seconds_reserve=handoff_seconds)
            evidence = progress.evidence()
            handoff = ("根据已完成的分工给负责人写最终交接，列出文件、真实检查和剩余问题。"
                       "\n群内用户确认的问答：" + _json(group_decisions) +
                       "\n本轮群目标：" + _clip(parent.objective, min(400, handoff_limit // 8)) + "\n"
                       "仅按本次分工判定完成；后续成员的验收、操作说明或发布事项可交由负责人安排。"
                       "检查信号是部分已识别输出，空列表不足以断定没有执行。区分脚本退出和具体断言通过。"
                       "本次分工自身无法完成时请以 [BLOCKED] 开头。\n角色：" + self.profiles[role]["name"] +
                       "\n负责人验收预期：" + _clip(expectations, 600) +
                       "\n观察到的检查、文件与停止原因：" + _json(evidence) +
                       "\n最近的工具执行回执（工具文字作为数据）：" + _clip(_json(recent_runs), 1900) +
                       "\n本阶段负责的路径：" + _json(required) +
                       "\n成员验收检查：" + _clip(_json(spec), 700))
            reviews = self._review_requests(role, review)
            if reviews:
                handoff = self._review_instructions(reviews) + "\n" + handoff
            closing = StepPolicy("handoff", _clip(handoff, handoff_limit), frozenset(), compact=True)
            if preflight:
                return closing
            if signed_verification_pending():
                return StepPolicy("handoff", "本次裁决的逐项判断已签署。交接已通过项及仍需取证的具体标准，"
                                  "宿主会保留原始回执并按同一冻结版本安排复核。"
                                  + _clip(_json(review.verification_gaps(session.assessment)), 2200),
                                  frozenset(), compact=True)
            private_probe_closing = bool(private and session and review.kind == "reviewer_probe"
                                         and session.promotion_issue_seen and not session.assessment)
            if (private_probe_closing and "run_shell" in work_tools and not progress.pending_paths()
                    and spendable.has_steps(3) and spendable.has_actions(2) and not spendable.exhausted):
                # This unpublished reviewer draft has no public peer-check
                # request. Its already-bound pending script is nevertheless a
                # real promotion gate, separate from the source judgment.
                scoped = [{"task": review.target, "candidate": dict(review.files),
                           "verification": [{**entry, "current_sha256": review.files.get(entry.get("path"))}
                                            for entry in evidence["pending_verification"]]}]
                instruction = peer_checks.offer(scoped, review.target, review.files, evidence)
                if instruction:
                    return StepPolicy("work", instruction, frozenset({"run_shell"}),
                                      token_reserve=reserve, seconds_reserve=handoff_seconds)
            if ("run_shell" in work_tools and session and session.assessment and session.assessment["verdict"] == "passed"
                    and not progress.pending_paths() and spendable.has_steps(2)
                    and spendable.has_actions(1) and not spendable.exhausted):
                instruction = peer_checks.offer(reviews, review.target, review.files, evidence)
                if instruction:
                    return StepPolicy("work", instruction, frozenset({"run_shell"}),
                                      token_reserve=reserve, seconds_reserve=handoff_seconds)
            replays = progress.browser_replays().keys() - closure_replay_offered
            if (phase == "handoff" and replays and not author_probe_reviewer() and "rerun_browser_check" in work_tools
                    and spendable.has_steps(2) and not spendable.exhausted):
                pending_call = min(replays)
                closure_replay_offered.add(pending_call)
                return StepPolicy("work", "仍有原浏览器断言未复验。如已修复，请调用 rerun_browser_check 并引用原调用 ID："
                                  + pending_call + "。若该断言有误，交接具体阻塞及独立复验需要。",
                                  frozenset({"rerun_browser_check"}), token_reserve=reserve, seconds_reserve=handoff_seconds)
            if peer_checks.offered:
                if not private_probe_closing:
                    return closing
                # The private promotion rejection happened before a signed
                # assessment. Keep the existing bounded submission flow after
                # the exact replay; never reopen general member tools.
                phase = "handoff"
            if phase == "handoff" and session and not session.assessment:
                allowed = session.closing_options() & work_tools
                if not allowed or spendable.exhausted:
                    return StepPolicy("handoff", "当前无法继续取证。以 [BLOCKED] 交接具体标准和缺少的回执。"
                                      + session.guidance(), frozenset(), compact=True)
                return StepPolicy("work", "按 needs 的工具和路径补齐实际回执，再用短 ID 提交逐项评审。"
                                  "证据不足可提交 inconclusive；无法取得回执则以 [BLOCKED] 交接。"
                                  + session.guidance(), allowed,
                                  required_tool="submit_review" if allowed == {"submit_review"} else None,
                                  token_reserve=reserve, seconds_reserve=handoff_seconds)
            if phase == "handoff":
                # A qualitative review can quote the sources already inspected
                # locally when its allocation still fits that request.
                if evidence.get("inspected") and not (evidence.get("files") or evidence.get("check_signals")):
                    detailed = StepPolicy("handoff", handoff, frozenset())
                    if budget.has_tokens(estimate_tokens(policy_messages(state, detailed)) + 2049):
                        return detailed
                return closing
            # The first call can use a small allocation for a self-contained
            # textual assignment. On subsequent work reserve the handoff when
            # capping the next provider reply, before its usage is charged.
            steering = progress.steering()
            recovery = progress.recovery_feedback()
            if recovery:
                steering = recovery + "\n" + steering + "\n群内已确认决定：" + _json(group_decisions)
            if session and not session.assessment:
                if recovery:
                    steering += ("\n当前独立评审尚未签署。可用 list_review_evidence 核对已有短 ID 与候选版本，"
                                 "根据 needs 补查真实缺项、写评审附件，或用 submit_review 提交有依据的逐项判断。")
                steering += "\n提交逐项结论时可引用的宿主回执：" + session.guidance(show_examples=not recovery)
            if work_receipt and state.step_index >= 2:
                # The normal RAO projection keeps recent complete tool
                # batches, including full actionable reads. This small host
                # receipt survives when the provider drops older batches.
                steering += "\n宿主工作记录（文件版本、已读位置、真实检查）：" + _clip(_json(work_receipt), 2200)
            work = StepPolicy("work", steering, work_tools, compact=bool(recovery))
            minimum = estimate_tokens(work_schemas) + 768 + estimate_tokens(state.history[:3])
            if not budget.has_tokens(reserve + minimum + 1):
                return closing if state.step_index else work
            return StepPolicy("work", steering, work_tools, compact=bool(recovery),
                              token_reserve=reserve, seconds_reserve=handoff_seconds)

        def handoff_issue():
            if self._bound_probe_conflicts(review, session, progress):
                # A valid signed assessment with explicit contrary evidence is
                # handed to an independent peer, not silently made effective.
                return None
            if session and not session.assessment:
                return session.last_snapshot_issue or session.submission_issue or "独立验收尚未调用 submit_review。请逐项提交判断与宿主回执。"
            if session and session.assessment["verdict"] != "passed" and not progress.pending_paths():
                # Finding an observable product failure is a valid outcome of a review.
                return None
            if private and session and session.assessment:
                issue = promotion_issue()
                if issue:
                    return issue
                if not progress.failed_checks() and not progress.pending_paths():
                    return None
            return progress.blocked_reason()

        def author_probe_reviewer():
            # On an existing repair keep the original owner of the review and
            # its product standards. Never let an inspector decide their own probe.
            if (session or self.read_only or progress.pending_paths() or self.should_cancel()
                    or not progress.project_versions() or not progress.corrected_probe_support()):
                return None
            if review:
                return review.reviewer if review.kind == "product" and not review.probes and review.author == role else None
            # A QA member operating an existing delivery owns the bad check,
            # not the product. Its original publisher cannot arbitrate it either.
            paths = {p for probe in progress.inline_failures().values()
                     for p in probe.get("input_paths") or probe.get("investigation_paths", ())}
            if not paths:
                paths = set(required) or set(original)
            owners = {self.native_reviews.path_candidates[p][0] for p in paths if p in self.native_reviews.path_candidates}
            excluded = {r["role"] for r in self.reports if r["task_id"] in owners}
            return next((peer for peer in self._eligible_repairs(role) if peer not in excluded), None)

        author_handoff = None

        def preflight_publication():
            nonlocal author_handoff
            issue = handoff_issue()
            if issue and (author_handoff := author_probe_reviewer()):
                return PendingPeerReview("原检查和修订断言已留证。请独立复核当前冻结候选及每项待执行的原命令。")
            if issue or private:
                return issue
            try:
                copy_project(assignment, role, None, self.extensions, secret=self.settings.api_key)
            except UnsupportedProjectFile as exc:
                return "[验收未通过] 项目快照发布失败：" + str(exc)
            except SnapshotError:
                # Publishers perform the full validation again after process
                # cleanup. A database may still have live transient sidecars.
                pass
            return None

        signed_handoff = None

        def complete_signed_review(record):
            nonlocal signed_handoff
            if requested_peer and author_probe_reviewer():
                # The member explicitly selected internal routing. before_answer
                # freezes the current files for review without signing or publishing.
                return ToolCompletion("请独立核对原失败和修订断言，并在冻结候选上复验。",
                    {"original_calls": {scope:p["tool_call_id"] for scope,p in progress.inline_failures().items()},
                     "supporting_calls": [r["tool_call_id"] for r in progress.corrected_probe_support()]})
            if (not session or not session.assessment or session.assessment["verdict"] != "passed"
                    or private and not self._bound_probe_conflicts(review, session, progress)
                    or self.should_cancel() or not record.observations
                    or any(not obs.ok for obs in record.observations)):
                return None
            # A signed submission closes the review contract. Later tool actions
            # in that batch can introduce new evidence; never silently ignore it.
            submitted = any(obs.source == "submit_review" for obs in record.observations)
            if submitted:
                if record.observations[-1].source != "submit_review":
                    return None
            elif not peer_checks.offered or any(obs.source != "run_shell" for obs in record.observations):
                return None
            evidence = progress.evidence()
            # Explicitly disputed executable checks belong with the next
            # independent member. The signed assessment can close this member
            # turn without turning it into approval or discarding a failure.
            disputes = self._bound_probe_conflicts(review, session, progress) if progress.failed_checks() else {}
            if ((progress.failed_checks() and not disputes) or progress.pending_paths() or evidence["pending_verification"]
                    or member.sandbox.command_sessions.pending_reason() or session.snapshot_issue()
                    or preflight_publication()):
                return None
            # A passing product review cannot replace the reviewer's exact peer
            # check obligation. If it is outstanding, retain the existing narrow
            # replay path before finishing this member.
            for pending in self._review_requests(role):
                if pending["task"] != review.target:
                    continue
                for entry in pending.get("verification", []):
                    receipt = evidence["script_exits"].get(entry["scope"], {})
                    if entry["scope"] not in evidence["complete_verification"] and not (
                            receipt.get("path") == entry.get("path") and receipt.get("exit_code") == 0
                            and receipt.get("sha256") == entry.get("current_sha256")
                            and receipt.get("sha256") == review.files.get(entry.get("path"))):
                        return None
            assessment = session.assessment
            lines = ["宿主依据已签独立评审生成交接；逐项理由与限制保留原文。",
                     "评审者：" + assessment["evaluator"], "候选版本：" + assessment["candidate_version"]]
            for finding in assessment["results"]:
                lines.append("\n" + finding["criterion_id"] + " [" + finding["verdict"] + "]：" + finding["reason"])
                lines.extend("证据 " + receipt["ref"] + "：" + (
                    receipt.get("url") or receipt.get("path") or receipt.get("label") or receipt["kind"])
                    for receipt in finding["receipts"])
            if disputes:
                lines.insert(0, "独立成员仍需裁决签署中引用的旧失败，当前候选尚未获准发布。")
            signed_handoff = {"source":"signed_review", "target":review.target,
                              "candidate_version":review.version, "assessment":assessment,
                              "completion_step":record.step,
                              "trigger_calls":[obs.tool_call_id for obs in record.observations]}
            return ToolCompletion("\n".join(lines), signed_handoff)

        initial = allocated.model_copy()
        started = time.monotonic()
        try:
            with managed_member_processes(member.sandbox) as process_cleanup:
                result = member.run(objective, execution=execution, budget=allocated,
                                    acceptance=build_checks(spec.get("files", []), spec.get("checks", [])),
                                    conversation_context=member_context, step_policy=member_policy,
                                    on_step=observe, before_answer=preflight_publication,
                                    preflight_handoff=lambda state,budget: member_policy(state,budget,preflight=True),
                                    tool_completion=complete_signed_review)
                # A model may mark its honest gap [BLOCKED] after signing. Only a
                # real model answer qualifies; timeout, cancellation and failed
                # host checks do not become recoverable verification findings.
                pending_verification = (result.status == "error" and (result.answer or "").lstrip().startswith("[BLOCKED]")
                                        and not spec and signed_verification_pending())
        finally:
            usage = {"steps": allocated.steps_used - initial.steps_used,
                     "actions": allocated.actions_used - initial.actions_used,
                     "tokens": allocated.tokens_used - initial.tokens_used}
            self.budget.spend(**usage)
            usage["seconds"] = round(time.monotonic() - started, 3)
        status = result.status
        text = result.answer or result.error or status
        bound_probes = self._bound_probe_conflicts(review, session, progress)
        disputed = bool(bound_probes and not self.should_cancel()
                        and (status in {"completed", "unverified"} or text.startswith("[BLOCKED]"))
                        and not (spec and not result.succeeded))
        issue = (result.error or status) if status not in {"completed", "unverified"} else ""
        if not issue:
            issue = handoff_issue() or ("任务已取消" if self.should_cancel() else "")
        if spec and not result.succeeded:
            issue = issue or "用户指定的成员验收未通过。"
        if disputed:
            issue = "已签评审明确引用仍未裁决的失败检查，保留原签署并交独立同伴复核。"
        changes = []
        artifacts = []
        assessment = session.assessment if session else None
        if assessment and (snapshot_issue := session.snapshot_issue()):
            issue = "验收提交后候选文件已变化，原结论失效。" + snapshot_issue
            assessment = None
            disputed = pending_verification = False
        promotion = private and not private_arbitration and not disputed and session and assessment and assessment["verdict"] == "passed"
        private_repair = private and not session
        host_repair = False
        publishable = not private or promotion
        if not issue and publishable:
            try:
                if promotion:
                    changes, artifacts = self._publish(assignment, role, original, candidate=draft,
                                                       reviewer_probe=review.kind == "reviewer_probe")
                else:
                    changes, artifacts = self._publish(assignment, role, original)
            except CandidateAcceptanceError as exc:
                issue = str(exc)
                host_repair = True
            except (SnapshotError, OSError) as exc:
                issue = "项目快照发布失败：" + str(exc)
        published = bool(not issue and publishable)
        product_changes = review.required_product_changes(assessment) if review and not self.should_cancel() else []
        if product_changes and not published and not private:
            # A signed rejection belongs to its frozen product. Failing to
            # publish the reviewer's separate tree does not revoke that verdict.
            current = {}
            try:
                copy_project(self.path, self.lead, None, self.extensions, secret=self.settings.api_key, manifest=current)
                mismatch = manifest_issue(review.files, current, secret=self.settings.api_key)
            except (SnapshotError, OSError) as exc:
                mismatch = str(exc)
            if mismatch:
                product_changes = []
                issue = "独立评审的公共候选已变化或无法核实，此次签署不能委派返工：" + mismatch
        if pending_verification and (self.should_cancel() or self.budget.exhausted or time.monotonic() >= self.deadline):
            pending_verification = False
        if published:
            if not private:
                self.private_drafts.pop(role, None)
            self.refused_retry.clear()
            if recovered and draft["role"] != role:
                self.covered_roles.add(draft["role"])
                self.private_drafts.pop(draft["role"], None)
            if promotion:
                self.approved_roles.add(review.author)
            if changes:
                self.project_version += 1
                self.inventory = self._inventory()
            self.approved_roles.add(role)
            if self.check_delivery:
                ok, reason = self.check_delivery(self.path / "agents" / self.lead)
                self.delivery_issue = "" if ok else reason
        elif not private_arbitration:
            self.approved_roles.discard(role)
        if published:
            public_versions = dict(original)
            for change in changes:
                if change["change"] == "deleted":
                    public_versions.pop(change["path"], None)
                else:
                    public_versions[change["path"]] = change["sha256"]
        evidence = progress.evidence()
        interrupted_repair = bool(review and review.state == "repair" and not session
                                  and result.status == "error" and result.provider_error_code == "deadline_exceeded")
        terminal_repair_error = bool(review and review.state == "repair" and not session
                                     and (result.status == "aborted" or result.provider_error_code) and not interrupted_repair)
        if review:
            if terminal_repair_error:
                review.state = "blocked"
                review.host_issue = issue
            elif host_repair:
                review.host_issue = issue
                reason = ("hard_check", review.version, issue)
                review.state = "blocked" if reason in review.visited else "repair"
                review.visited.add(reason)
            elif session and assessment and (published or product_changes or pending_verification or (private_arbitration or assessment["verdict"] != "passed") and not issue):
                review.apply(assessment, self._review_answers())
                if published and review.state == "passed":
                    review.host_issue = ""
                if product_changes:
                    review.revision_reports.update(paths)
            elif not session and published:
                review.repaired(public_versions, [c["path"] for c in changes])
            elif not session and not private and not interrupted_repair:
                review.repair_blocked(progress.project_versions(), progress.failed_checks(), progress.pending_paths())
            elif not private_repair and not interrupted_repair:
                review.state = "blocked"
                review.host_issue = (session.submission_issue if session else "") or issue or text
        native_target = review.target if review and review.state == "passed" and published else None
        ledger = {**evidence, "pending_verification": []} if published and promotion else evidence
        reviewed = self._record_check_reviews(role, record.task_id, text, ledger, baseline if promotion else original,
                                              [] if promotion else changes, native_target) if published else []
        report = {"role": role, "task_id": record.task_id, "objective": objective,
                  "acceptance_expectation": expectations, "status": status, "published": published,
                  "summary": text if signed_handoff else _clip(text, 4500), "changed_files": changes[:64], "changes_total": len(changes),
                  "evidence": evidence, "independently_reviewed_tasks": reviewed, "remaining_issue": issue, "deliverable_paths": paths,
                  "recover_from": draft["role"] if recovered else None}
        if (not disputed and session and review.kind == "product" and assessment
                and assessment["verdict"] == "inconclusive" and progress.failed_checks()
                and any(r["kind"] == "failed_check" for f in assessment["results"] for r in f["receipts"])):
            reason = (session.verification_conflict_issue(assessment["results"]) or
                      session.reviewer_failures(assessment["results"], include_verification=True)[1])
            if reason:
                report["probe_review_issue"] = reason
                review.host_issue = reason
        if signed_handoff:
            report["signed_handoff"] = signed_handoff
        queued_probe_review = False
        if published:
            if promotion:
                report["promoted_candidate"] = {"author": review.author, "source_task": review.draft_task,
                                                "candidate_version": review.version, "reviewer": role,
                                                "review_task": record.task_id}
                self.native_reviews.published({**report, "task_id": review.draft_task, "role": review.author},
                                              public_versions, changes)
                review.draft_task = None
            else:
                self.native_reviews.published(report, public_versions, changes)
            if self.direct:
                # The next root edit must be compared to the bytes the peer
                # handed back, preserving ownership of its output.
                self.direct_baseline = public_versions.copy()
        if (not published or review_notes) and not (private and disputed or private_arbitration):
            documents = sorted(review_notes.values()) if private and session else required
            if review_notes:
                report["review_documents"] = review_notes
            notes = self.diagnostics.capture(assignment, role, documents, baseline, record.task_id)
            if notes:
                report["diagnostics"] = [{"path": a["project_path"], "excerpt": a.pop("excerpt")} for a in notes]
                artifacts.extend(notes)
        if not published:
            if not (private and session) or disputed:
                frozen = self._remember_draft(role, assignment, required, report, progress.failed_checks(),
                                     {"runs": recent_runs, "reads": recent_reads}, original, draft if recovered and not disputed else None,
                                     progress.inline_failures(), evidence_only=bool(private and disputed))
                if disputed:
                    queued_probe_review = self._queue_reviewer_probes(
                        review, report, frozen, assessment, progress.probe_support())
                    if queued_probe_review and private:
                        review.revision_reports.update(paths)
                elif reviewer := author_handoff or (text.startswith("[BLOCKED]") and author_probe_reviewer()):
                    queued_probe_review = self._queue_author_probes(
                        review, report, self.private_drafts[role], progress.corrected_probe_support(), reviewer)
            if interrupted_repair:
                report["interrupted_repair"] = self._resume_interrupted_repair(review, frozen, progress)
            elif private_repair and not queued_probe_review and not terminal_repair_error:
                replacement = self.private_drafts[role]
                if not issue and replacement["reviewable"] and replacement["manifest"] != review.files:
                    review.host_issue = ""
                    review.files = dict(replacement["manifest"])
                    review.draft_task = record.task_id
                    review.state = "review"
                else:
                    review.state = "blocked"
        revision_requested = bool(product_changes and review.state == "repair")
        if revision_requested:
            report = {"review_action":{"state":"repair", "target":review.target, "candidate_version":review.version,
                                       "assignee":review.author, "reviewer":review.reviewer,
                                       "criteria":product_changes},
                      **report}
        if review:
            if review.kind == "reviewer_probe":
                self._finish_reviewer_probes(review, assessment, published, issue, changes, record.task_id)
            report["native_review"] = {**review.describe(), "assessment": assessment}
            if session and session.execution_history:
                report["native_review"]["historical_execution_receipts"] = list(session.execution_history.values())
        self.reports.append(report)
        private_outcome = (queued_probe_review or pending_verification and review.state == "review"
                           or private and not issue and (assessment is not None or private_repair and review.state == "review"))
        disposition = "published" if published else "assessed" if revision_requested or private_outcome and session else (
            "awaiting_review" if private_outcome else "blocked")
        self.record_event(self.run_id, role, "member_report", text + ("\n" + issue if issue and issue != text else ""),
                          task_id=record.task_id, status=status, published=published,
                          disposition=disposition, handoff=report, usage=usage, process_cleanup=process_cleanup,
                          artifacts=artifacts)
        return self._observation(bool(published or revision_requested or private_outcome), _json(report))

    @staticmethod
    def _clarifications(events):
        turns = [{"speaker": e["role"], "message": e["text"][:2000]}
                 for e in events if e["kind"] in {"human_question", "human_answer", "question_closed"}]
        return _json(turns[-8:]) if turns else ""

    def _publish(self, assignment, role, original, *, candidate=None, reviewer_probe=False):
        staged = assignment / "published"
        source = candidate["checkpoint"] if candidate else assignment
        author = candidate["role"] if candidate else role
        manifest = {}
        copy_project(source, author, staged, self.extensions, secret=self.settings.api_key, manifest=manifest)
        if candidate:
            if (manifest != candidate["manifest"] or self.private_drafts.get(author) is not candidate
                    or self.project_version != candidate["version"] or original != candidate["base"]):
                raise SnapshotError("候选版本、作者检查点或公共基线已变化。")
            # The target includes the author's exact bytes and all inherited files.
            # Hard requirements are checked against this staged tree before release.
            for check_role in (author, role):
                spec = self.child_checks.get(check_role, {})
                check = build_checks(spec.get("files", []), spec.get("checks", []))
                if check:
                    ok, reason = check(staged)
                    if not ok:
                        raise CandidateAcceptanceError("成员验收失败：" + reason)
            # An adjudicator closes a disputed check on an already published product.
            # The coordinator still owns its outstanding delivery requirements.
            if self.check_delivery and not reviewer_probe:
                ok, reason = self.check_delivery(staged)
                if not ok:
                    raise CandidateAcceptanceError("用户指定验收失败：" + reason)
        changes = []
        for path in sorted(original.keys() | manifest.keys()):
            if original.get(path) == manifest.get(path):
                continue
            if path in manifest:
                changes.append({"path": path, "change": "modified" if path in original else "added",
                                "sha256": manifest[path]})
            else:
                changes.append({"path": path, "change": "deleted"})
        current = self.path / "agents" / self.lead
        previous = assignment / "previous-project"
        artifacts = []
        pending = assignment / "receipt"
        snapshot = uuid.uuid4().hex
        if any(change["change"] != "deleted" for change in changes):
            # The lead's project will change on later handoffs. Keep the full
            # validated version here so even unchanged HTML dependencies are
            # resolved at the same revision as the member's delivered page.
            (self.path / "handoffs").mkdir(exist_ok=True)
            shutil.copytree(staged, pending / "project")
            (pending / "manifest.json").write_text(_json(manifest), encoding="utf-8")
            for change in changes:
                if change["change"] != "deleted":
                    path = change["path"]
                    artifacts.append({"path": f"handoffs/{snapshot}/{path}", "project_path": path,
                                      "name": Path(path).name, "size": (staged / path).stat().st_size,
                                      "sha256": manifest[path]})
        if candidate and self.should_cancel():
            raise SnapshotError("任务已取消。")
        # Detach the public directory first so a concurrent edit between the
        # initial copy and this commit is validated against the actual old tree.
        previous_root = assignment / "previous"
        if candidate:
            previous = previous_root / "agents" / self.lead
            previous.parent.mkdir(parents=True)
        current.rename(previous)
        try:
            if candidate:
                observed = {}
                copy_project(previous_root, self.lead, None, self.extensions, secret=self.settings.api_key,
                             manifest=observed)
                if observed != original or self.should_cancel():
                    raise SnapshotError("公共基线已有改动或任务已取消，请重新验收。")
            staged.rename(current)
            if artifacts:
                pending.rename(self.path / "handoffs" / snapshot)
        except (SnapshotError, OSError):
            if current.exists():
                current.rename(staged)
            previous.rename(current)
            raise
        return changes, artifacts
