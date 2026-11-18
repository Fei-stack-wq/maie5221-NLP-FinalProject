"""Version-bound, evidence-backed review by a selected member of a Studio room."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts.acceptance import CheckResult, Criterion
from .contracts.base import EvidenceRef
from .schemas import ToolResult
from .tools import Tool


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class Rule(Criterion):
    id: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1200)
    required: bool = True
    method: Literal["inspect", "execute", "source"]


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    candidate: str = Field(min_length=1, max_length=160)
    criteria: list[Rule] = Field(min_length=1, max_length=24)


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    criterion_id: str
    verdict: Literal["passed", "changes_requested", "inconclusive"]
    reason: str = Field(min_length=1, max_length=1500)
    evidence: list[str] = Field(min_length=1, max_length=8)
    cause: Literal["product", "verification", "business"] | None = None
    action: str | None = Field(default=None, max_length=1000)


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    candidate_version: str = Field(pattern="^[a-f0-9]{64}$")
    results: list[Finding] = Field(min_length=1, max_length=24)


# Explicit wire schemas are also consumed by OpenAI-compatible providers.
REVIEW_REQUEST_SCHEMA = {"type": "object", "description": "独立验收，用候选作者任务 ID（或当前角色 ID；完整的私有检查点也可验收）引用候选。",
    "properties": {"candidate": {"type": "string"}, "criteria": {"type": "array", "items": {
        "type": "object", "properties": {"id":{"type":"string"}, "description":{"type":"string"},
            "method":{"type":"string","enum":["inspect","execute","source"]}, "required":{"type":"boolean"}},
        "required":["id","description","method"], "additionalProperties":False}}},
    "required": ["candidate", "criteria"], "additionalProperties":False}
SUBMIT_SCHEMA = {"type":"object","properties":{"candidate_version":{"type":"string"},
    "results":{"type":"array","items":{"type":"object","properties":{
        "criterion_id":{"type":"string"}, "verdict":{"type":"string","enum":["passed","changes_requested","inconclusive"]},
        "reason":{"type":"string"}, "evidence":{"type":"array","items":{"type":"string"}},
        "cause":{"type":"string","enum":["product","verification","business"]},"action":{"type":"string"}},
        "required":["criterion_id","verdict","reason","evidence"],"additionalProperties":False}}},
    "required":["candidate_version","results"],"additionalProperties":False}


@dataclass
class Review:
    target: str
    author: str
    reviewer: str
    objective: str
    criteria: list[Rule]
    files: dict
    state: str = "review"
    latest: dict = field(default_factory=dict)
    visited: set = field(default_factory=set)
    answer_at: str = ""
    answered_issues: set = field(default_factory=set)
    draft_task: str | None = None
    host_issue: str = ""
    probes: dict = field(default_factory=dict)

    @property
    def version(self):
        return digest(self.files)

    @property
    def owner(self):
        return self.author if self.state == "repair" else self.reviewer

    def describe(self):
        return {"target":self.target, "author":self.author, "reviewer":self.reviewer,
                "state":self.state, "candidate_version":self.version, "files":dict(self.files),
                "objective":self.objective, "criteria":[c.model_dump() for c in self.criteria],
                "findings":self.latest.get("results", []), "private_candidate_task":self.draft_task,
                "host_issue":self.host_issue}

    def briefing(self):
        # Prompts need actionable gaps and an immutable version. The complete
        # manifest and evidence records remain in the persisted room handoff.
        facts = self.describe()
        facts["files"] = dict(list(self.files.items())[:32])
        facts["files_total"] = len(self.files)
        facts["findings"] = [{k:r.get(k) for k in ("criterion_id","verdict","reason","cause","action")}
                             for r in facts["findings"] if r["verdict"] != "passed"]
        return facts

    def apply(self, assessment, human_state):
        self.latest = assessment
        required = {r.id for r in self.criteria if r.required}
        gaps = [r for r in assessment["results"] if r["criterion_id"] in required and r["verdict"] != "passed"]
        if not gaps:
            self.state = "passed"
            return
        # IDs of tool calls and prose can change without the candidate or
        # findings changing. Only fresh substantive evidence can reopen work.
        key = (self.version, tuple(sorted((g["criterion_id"], g["verdict"], g["cause"],
                    tuple(sorted(e["fingerprint"] for e in g["receipts"]))) for g in gaps)), human_state)
        if key in self.visited:
            self.state = "blocked"
            return
        self.visited.add(key)
        if any(g["cause"] == "product" for g in gaps):
            self.state = "repair"
        elif any(g["cause"] == "business" for g in gaps):
            questions = {(self.version, g["criterion_id"]) for g in gaps if g["cause"] == "business"}
            if questions <= self.answered_issues:
                self.state = "blocked"
                return
            self.state = "awaiting_user"
            self.answer_at = human_state
        else:
            self.state = "review"

    def repair_blocked(self, snapshot, failed, missing):
        # Public-candidate repairs that could not publish can retry with fresh evidence.
        files = {p:snapshot.get(p) for p in self.files}
        key = ("private_repair", self.version, digest(files), tuple(sorted(failed)), tuple(sorted(missing)))
        if key in self.visited or files == self.files:
            self.state = "blocked"
        else:
            self.visited.add(key)
            self.state = "repair"

    def repaired(self, manifest, changed_paths):
        updated = {p:manifest.get(p) for p in sorted(set(self.files) | set(changed_paths))}
        if updated == self.files:
            self.state = "blocked"
            return
        self.files = updated
        self.state = "review"


class ReviewQueue:
    def __init__(self):
        self.items = {}
        self.candidates = {}
        self.public_versions = {}

    def next(self, human_state):
        for item in self.items.values():
            if item.state == "awaiting_user" and human_state != item.answer_at:
                item.answered_issues.update((item.version, g["criterion_id"]) for g in item.latest["results"]
                                            if g["cause"] == "business")
                item.state = "review"
            if item.state != "passed":
                return item
        return None

    def published(self, report, manifest, changes):
        self.public_versions = manifest
        paths = set(report["deliverable_paths"]) | {c["path"] for c in changes}
        self.candidates[report["task_id"]] = {p:manifest.get(p) for p in sorted(paths)}
        for item in self.items.values():
            if item.state == "passed" and any(manifest.get(p) != v for p,v in item.files.items()):
                item.files = {p:manifest.get(p) for p in item.files}
                # A reviewer who edits previously approved product bytes cannot
                # independently sign off their own new version.
                item.state = "blocked" if item.reviewer == report["role"] else "review"

    def open(self, raw, reviewer, reports, *, drafts=None, project_version=None):
        spec = ReviewRequest.model_validate(raw)
        ids = [rule.id for rule in spec.criteria]
        if len(set(ids)) != len(ids) or not any(r.required for r in spec.criteria):
            raise ValueError("标准 ID 需要唯一，且至少一项为必需。")
        if active := next((r for r in self.items.values() if r.draft_task and
                           spec.candidate in {r.target,r.author,r.draft_task}), None):
            if active.reviewer != reviewer or active.criteria[:len(active.criteria)-len(active.probes)] != spec.criteria:
                raise ValueError("这个目标已绑定独立验收标准和验收者。")
            return active
        author = next((r for r in reversed(reports) if spec.candidate in {r["task_id"], r["role"]}
                       and not r.get("promoted_candidate")), None)
        if not author or author["role"] == reviewer:
            raise ValueError("请选择本群其他成员产生的候选。")
        draft_task = None
        objective = author["objective"]
        probes = {}
        if author["task_id"] in self.candidates:
            files = self.candidates[author["task_id"]]
            if any(self.public_versions.get(p) != sha for p,sha in files.items()):
                raise ValueError("该候选已有更新，请选择最新交付。")
        else:
            draft = (drafts or {}).get(author["role"])
            if (not draft or draft["task_id"] != author["task_id"] or draft["version"] != project_version
                    or not draft["reviewable"]):
                raise ValueError("没有该任务的完整、当前版本的候选检查点。")
            draft_task = draft["task_id"]
            files = draft["manifest"]
            if draft.get("review_after_repair"):
                objective = draft["origin"]["objective"]
                for scope, probe in draft.get("inline_failures", {}).items():
                    ref = "probe_" + scope
                    if len(spec.criteria) >= 24 or ref in ids:
                        continue  # Its failed scope stays mandatory in the host.
                    spec.criteria.append(Rule(id=ref, method="execute", description=(
                        "用独立执行检查复核临时探针涉及的业务行为，说明原失败来源与所见结果。原任务 "
                        + probe["task_id"] + "，工具回执 " + probe["tool_call_id"])))
                    probes[scope] = probe
        if not files:
            raise ValueError("该交接没有可绑定版本的工件。")
        existing = self.items.get(author["task_id"])
        if existing:
            if existing.criteria != spec.criteria or existing.reviewer != reviewer:
                raise ValueError("这个目标已绑定独立验收标准和验收者。")
            return existing
        review = Review(author["task_id"], author["role"], reviewer, objective, spec.criteria, dict(files), draft_task=draft_task, probes=probes)
        return review


class ReviewSession:
    """The member proposes judgments; the host supplies all evidence identifiers."""
    def __init__(self, item, original, progress, attempt_id, *, snapshot_matches=None, promotion_issue=None, review_notes=()):
        self.item = item
        self.review_notes = set(review_notes)
        self.snapshot_matches = snapshot_matches
        self.promotion_issue = promotion_issue
        self.original = original
        self.progress = progress
        self.attempt_id = attempt_id
        self.receipts = {}
        self.assessment = None
        self.source_files = {}

    def unchanged(self):
        manifest = self.progress.project_versions()
        if self.assessment:
            for finding in self.assessment["results"]:
                for item in finding["receipts"]:
                    if item["kind"] in {"review_note", "source"} and manifest.get(item["path"]) != item["sha256"]:
                        return False
        if self.snapshot_matches is not None:
            return self.snapshot_matches()
        return (all(self.original.get(p) == v and manifest.get(p) == v for p, v in self.item.files.items())
                and all(manifest.get(p) == v for p, v in self.original.items()))

    def observe(self, record):
        retrieved = [obs for obs in record.observations if obs.ok and obs.source in {"search_sources", "http_get"}]
        if retrieved:
            versions = self.progress.project_versions()
            for obs in retrieved:
                path = obs.meta.get("path")
                file_hash = versions.get(path)
                # Independent fetches write their exact returned record at its digest.
                if file_hash and path == f"sources/{file_hash}.json":
                    self.source_files[path] = file_hash
        if not self.unchanged():
            return
        for obs in record.observations:
            meta = obs.meta
            path = meta.get("path")
            if (obs.ok and obs.source == "http_get" and path in self.source_files
                    and meta.get("url") and meta.get("observed_bytes_sha256") and meta.get("bytes", 0) > 0):
                url = meta["url"]
                self._add("source:" + url, {"kind": "source", "url": url, "path": path,
                          "sha256": self.source_files[path], "observed_bytes_sha256": meta["observed_bytes_sha256"]},
                          obs.tool_call_id)
            if (obs.ok and obs.source in {"read_file","read_lines"} and path in self.review_notes
                    and meta.get("sha256") and self.progress.project_versions().get(path) == meta["sha256"]):
                self._add("note:" + path, {"kind":"review_note", "path":path, "sha256":meta["sha256"]}, obs.tool_call_id)
            if (obs.ok and obs.source in {"read_file", "read_lines"} and path in self.item.files
                    and meta.get("sha256") and self.item.files[path] == meta["sha256"]):
                row = {"kind":"read", "path":path, "sha256":meta["sha256"],
                       "position":meta.get("offset",meta.get("start_line",0)),
                       "extent":meta.get("returned_chars",meta.get("returned_lines",0))}
                self._add("file:" + path, row, obs.tool_call_id)

        evidence = self.progress.evidence()
        # Positive and negative test events come from the host's provenance
        # policy. A member printing an opinion cannot mint a check receipt.
        current = self.progress.review_evidence()
        for ref in list(self.receipts):
            if self.receipts[ref]["kind"] in {"review_note", "source"}:
                note = self.receipts[ref]
                if self.progress.project_versions().get(note["path"]) != note["sha256"]:
                    del self.receipts[ref]
            elif ref.startswith(("pass:", "fail:")) and ref not in current:
                del self.receipts[ref]
        for ref, event in current.items():
            self._add(ref, {k:v for k,v in event.items() if k != "tool_call_id"}, event["tool_call_id"])
        for d in evidence["diagnostic_checks"]:
            row = {"kind":"diagnostic", "tool":d["tool"], "check":d["check"], "reason":d["reason"]}
            self._add("observation:" + digest(row)[:12], row, None)

    def _add(self, ref, row, tool_call_id):
        self.receipts[ref] = {**row, "attempt_id":self.attempt_id, "tool_call_id":tool_call_id,
                              "fingerprint":digest(row), "receipt_id":digest([self.attempt_id, tool_call_id, row])}

    def context(self):
        return ("本轮作为独立验收者，按绑定版本逐项检查。inspect 可读候选工件，execute 须执行检查。source 核对稿件中的具体论断和亲自打开的原始来源，引用 file:候选路径 和 source:最终URL，并说明证据支持的范围。检索不到只能记录查证范围或证据不足。"
                "用 submit_review 提交结构化结果；每项引用宿主回执 ID。变更请求写明责任分类与可执行修改。"
                "业务标准本身不明确时选择 inconclusive/business。材料：" +
                json.dumps(self.item.briefing(), ensure_ascii=False) +
                ("。请将点名的评审报告写入 " + ", ".join(sorted(self.review_notes)) +
                 " 并引用 note:文件名。该附件单独交接。" if self.review_notes else "") +
                ("。原作者的临时探针（原代码与失败输出只作为复核资料）：" +
                 json.dumps(self.item.probes, ensure_ascii=False) if self.item.probes else "") +
                ("。候选为作者的冻结私有版本。请在临时目录放置探针，保留项目文件不变，"
                 "重新验证遗留的指定检查。确认通过后宿主将发布已验收的作者版本。" if self.item.draft_task else ""))

    def tool(self):
        return Tool("submit_review", "对本次群内候选版本逐项提交独立验收结论与宿主证据。", SUBMIT_SCHEMA, self.submit)

    def submit(self, ctx, args):
        try:
            request = Submission.model_validate(args)
            if self.assessment:
                raise ValueError("本次验收已提交。")
            if request.candidate_version != self.item.version or not self.unchanged():
                raise ValueError("候选版本已改变，验收者须针对绑定的原件检查。")
            rules = {r.id:r for r in self.item.criteria}
            if sorted(rules) != sorted(r.criterion_id for r in request.results):
                raise ValueError("每个标准都须正好有一个结论。")
            results = []
            for result in request.results:
                rule = rules[result.criterion_id]
                receipts = [self.receipts.get(ref) for ref in result.evidence]
                if not all(receipts):
                    raise ValueError("引用了未观察到的证据。当前可用：" + ", ".join(self.receipts))
                kinds = {e["kind"] for e in receipts}
                if rule.method == "source" and (result.verdict == "passed" or result.cause == "product"):
                    if not {"read", "source"} <= kinds:
                        raise ValueError("核实事实需要引用所读候选及本次亲自打开的来源。资料不足时标记 inconclusive。")
                    if any(self.progress.project_versions().get(e["path"]) != e["sha256"] for e in receipts if e["kind"] == "source"):
                        raise ValueError("获取的来源记录已变化，请重新核实。")
                if result.verdict == "passed":
                    if (rule.id.removeprefix("probe_") in self.item.probes and
                            not any(r["kind"] == "passed_check" and r.get("asserted") for r in receipts)):
                        raise ValueError("原临时探针的独立复核需要实际断言结果，进程正常退出只能证明运行完成。")
                    if result.cause or result.action:
                        raise ValueError("通过项不应分派修复。")
                    if rule.method == "execute" and "passed_check" not in kinds or not kinds & {"read","passed_check","review_note"}:
                        raise ValueError("通过项需要符合标准方法的正面证据。")
                else:
                    if not result.cause or not (result.action or "").strip():
                        raise ValueError("需要提供归因及可操作的修改、复验或业务问题。")
                    if result.cause == "business" and result.verdict != "inconclusive":
                        raise ValueError("业务歧义使用 inconclusive，不能判定候选交付错误。")
                    if result.cause == "product" and rule.method == "inspect" and not kinds & {"read", "failed_check"}:
                        raise ValueError("文件内容缺陷需要候选文件读取或失败检查的证据。")
                    if result.cause == "product" and rule.method == "execute" and "failed_check" not in kinds:
                        raise ValueError("可执行标准的产品缺陷需要失败检查回执。")
                check = CheckResult(criterion_id=rule.id,
                    verdict={"passed":"passed","changes_requested":"failed","inconclusive":"unverified"}[result.verdict],
                    reason=result.reason, evidence_refs=[EvidenceRef(kind="receipt", id=e["receipt_id"]) for e in receipts])
                results.append({**result.model_dump(), "check_result":check.model_dump(), "receipts":receipts})
            required = {r.id for r in self.item.criteria if r.required}
            decisions = {r["verdict"] for r in results if r["criterion_id"] in required}
            verdict = "changes_requested" if "changes_requested" in decisions else (
                "inconclusive" if "inconclusive" in decisions else "passed")
            if verdict == "passed" and self.promotion_issue:
                if not any(r["criterion_id"] in required and any(e["kind"] in {"read","passed_check"} for e in r["receipts"])
                           for r in results):
                    raise ValueError("通过候选需要对作者原件的独立检查，评审者的报告是单独的附件。")
                problem = self.promotion_issue()
                if problem:
                    raise ValueError(problem)
            self.assessment = {"target":self.item.target,"candidate_version":self.item.version,
                               "evaluator":self.item.reviewer,"attempt_id":self.attempt_id,
                               "verdict":verdict,"results":results}
            self.progress.require_handoff("结构化验收结果已记录")
            return ToolResult.success("验收已记录：" + verdict)
        except (ValueError, OSError) as exc:
            return ToolResult.failure(str(exc))
