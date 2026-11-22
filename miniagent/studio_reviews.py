"""Version-bound, evidence-backed review by a selected member of a Studio room."""
from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .acceptance import _relative_name
from .contracts.acceptance import CheckResult, Criterion
from .contracts.base import EvidenceRef
from .project_data import PROJECT_CACHE_DIRS
from .probe_coverage import corrected_assertion
from .schemas import ToolResult
from .studio_conversations import snapshot_location
from .tools import Tool


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class Rule(Criterion):
    id: str = Field(min_length=1, max_length=80, description="本次验收中的唯一标准 ID。沿用原标准时须保留原 ID 和定义。")
    description: str = Field(min_length=1, max_length=1200, description="说明该项要检查的具体结果。")
    required: bool = True
    method: Literal["inspect", "execute", "source"] = Field(
        description="inspect 检查文件，execute 验证实际运行行为，source 须独立打开外部网页核实事实。")
    artifact_paths: list[str] | None = Field(default=None,
        description="条款对应的冻结文件相对路径。")
    reference_path: str | None = Field(default=None, min_length=1, max_length=500, description=(
        "仅在 inspect 需要对照第二份原始记录时填写该记录的工作区规范相对路径。核对成稿自身的字数、哈希或结构时省略。"))

    @model_validator(mode="after")
    def check_local_reference(self):
        if self.artifact_paths and self.method != "execute":
            required_refs = len(self.artifact_paths) + int(self.method == "source" or bool(self.reference_path))
            if required_refs > 8:
                raise ValueError("该条款所需的回执数超过签署接口的单项上限 8。")
        if self.artifact_paths:
            if len(set(self.artifact_paths)) != len(self.artifact_paths) or any(
                _relative_name(p) != p or "\x00" in p for p in self.artifact_paths
            ):
                raise ValueError("artifact_paths 须为互不重复的规范相对路径。")
        if self.reference_path is not None:
            if self.method != "inspect":
                raise ValueError("本地原件对照请使用 method=inspect 并保留 reference_path。"
                                 "source 专用于需要亲自打开外部网页的事实核查。")
            if _relative_name(self.reference_path) != self.reference_path or "\x00" in self.reference_path:
                raise ValueError("reference_path 须为工作区中原始记录的规范相对路径。")
        return self


class ReviewRequest(BaseModel):
    """对指定候选进行独立验收。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    candidate: str = Field(min_length=1, max_length=160, description=(
        "填写候选作者的任务 ID 或当前角色 ID。引用同群已发布或继承的原件可用成稿路径、原任务 ID 或唯一的原作者标识。"
        "须由原作者以外的成员独立评审。"))
    criteria: list[Rule] = Field(default_factory=list, max_length=24, description=(
        "首次验收须至少给出一个 required=true 的标准，每个 ID 只用一次。复核同一已发布原件时，宿主保留未通过的"
        "必需项，可省略 criteria 以沿用这些项。新增要求用新 ID；用户指定来源核查的成稿须包含必需的 source 标准。"))


class CandidateLookupError(ValueError):
    """The requested artifact or independent reviewer is not a valid destination."""


class UnresolvedReviewEvidence(ValueError):
    """The cited evidence is missing, ambiguous or superseded."""


class InheritedCriteriaConflict(ValueError):
    """A coordinator needs the complete bound definitions to correct its request."""


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    criterion_id: str = Field(description="本轮绑定的验收标准 ID。")
    verdict: Literal["passed", "changes_requested", "inconclusive"] = Field(
        description="passed 时省略 cause 和 action。changes_requested 或 inconclusive 时两项都必须填写。")
    reason: str = Field(min_length=1, max_length=1500, description="简述该项依据与结论；长报告留在交接附件。")
    evidence: list[str] = Field(min_length=1, max_length=8,
                                description="引用 list_review_evidence 列出的回执 ID 或可唯一解析的别名；按本项方法取证。")
    cause: Literal["product", "verification", "business"] | None = Field(
        default=None, description="非通过项的归因。business 仅可搭配 inconclusive。通过项省略。")
    action: str | None = Field(default=None, max_length=1000,
                               description="非通过项必须给出具体修改、复验或待澄清事项。通过项省略。")


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    candidate_version: str = Field(pattern="^[a-f0-9]{64}$", description="从本轮验收上下文复制绑定版本的完整摘要。")
    results: list[Finding] = Field(min_length=1, max_length=24, description=(
        "每个绑定标准恰有一个结论，每个 ID 只提交一次。复验时可以沿用宿主已保留的结论，须补齐其余项。"))


def _review_tool_schema(model: type[BaseModel], collection: str) -> dict:
    """Inline the one nested item type so a review schema can itself be embedded in another tool."""
    schema = model.model_json_schema()
    array = schema["properties"][collection]
    definition = array["items"]["$ref"].removeprefix("#/$defs/")
    array["items"] = schema.pop("$defs")[definition]
    return schema


REVIEW_REQUEST_SCHEMA = _review_tool_schema(ReviewRequest, "criteria")
SUBMIT_SCHEMA = _review_tool_schema(Submission, "results")


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
    interrupted_checkpoints: dict = field(default_factory=dict)
    answer_at: str = ""
    answered_issues: set = field(default_factory=set)
    draft_task: str | None = None
    host_issue: str = ""
    probes: dict = field(default_factory=dict)
    required_source_paths: set[str] = field(default_factory=set)
    publication: dict = field(default_factory=dict)
    kind: str = "product"
    probe_parent: str | None = None
    probe_support: list = field(default_factory=list)
    probe_product_files: dict = field(default_factory=dict)
    probe_reference_inputs: dict = field(default_factory=dict)
    comparison_targets: set[str] = field(default_factory=set)
    peer_recheck: dict = field(default_factory=dict)
    revision_reports: set[str] = field(default_factory=set)
    covered_failed_sites: set = field(default_factory=set)
    author_requested: bool = False
    requirements: str = ""
    delivery_paths: tuple[str, ...] = ()
    contract: dict = field(default_factory=dict)

    def compare_with(self, reference):
        return (self.comparison_targets or {p for p, sha in self.files.items() if sha}) - {reference}

    def sourced_files(self, results):
        """Candidate bytes cited with a freshly retrieved source on a required passing finding."""
        source_rules = {rule.id for rule in self.criteria if rule.required and rule.method == "source"}
        files = {}
        for finding in results:
            if finding["criterion_id"] not in source_rules or finding["verdict"] != "passed":
                continue
            receipts = finding["receipts"]
            if any(e["kind"] == "source" for e in receipts):
                files.update({e["path"]: e["sha256"] for e in receipts if e["kind"] == "read"
                              and e.get("extent", 0) > 0 and self.files.get(e["path"]) == e["sha256"]})
        return files

    @property
    def version(self):
        return digest(self.files)

    @property
    def owner(self):
        return self.author if self.state == "repair" else self.reviewer

    def required_product_changes(self, assessment):
        if (not assessment or self.kind != "product" or assessment["verdict"] != "changes_requested"
                or assessment["target"] != self.target or assessment["candidate_version"] != self.version
                or assessment["evaluator"] != self.reviewer):
            return []
        required = {r.id for r in self.criteria if r.required}
        return [f["criterion_id"] for f in assessment["results"]
                if f["criterion_id"] in required and f["verdict"] == "changes_requested" and f["cause"] == "product"]

    def verification_gaps(self, assessment=None):
        decision = self.latest if assessment is None else assessment
        if self.state != "review" or decision.get("candidate_version") != self.version:
            return []
        required = {rule.id for rule in self.criteria if rule.required}
        gaps = [g for g in decision["results"] if g["criterion_id"] in required and g["verdict"] != "passed"]
        return gaps if all(g.get("cause") == "verification" for g in gaps) else []

    def describe(self):
        return {**({"acceptance_contract": self.contract or {"status":"pending", "delivery_paths":list(self.delivery_paths)}}
                   if self.requirements else {}),
                "target":self.target, "author":self.author, "reviewer":self.reviewer,
                "kind":self.kind, "probe_parent":self.probe_parent,
                "state":self.state, "candidate_version":self.version, "files":dict(self.files),
                "objective":self.objective, "criteria":[c.model_dump(exclude_none=True) for c in self.criteria],
                "findings":self.latest.get("results", []), "private_candidate_task":self.draft_task,
                "host_issue":self.host_issue, **({"publication":self.publication} if self.publication else {}),
                **({"review_report_paths":sorted(self.revision_reports)} if self.revision_reports else {}),
                **({"peer_recheck":self.peer_recheck} if self.peer_recheck.get("candidate_version") == self.version else {})}

    def briefing(self):
        # Prompts need actionable gaps and an immutable version. The complete
        # manifest and evidence records remain in the persisted room handoff.
        facts = self.describe()
        pending = [r for r in facts.pop("findings") if r["verdict"] != "passed"]
        # Tool results and coordinator close corrections are clipped. Keep the
        # identities and actions ahead of long objectives, manifests and rubrics.
        findings = []
        for result in pending:
            finding = {k: result.get(k) for k in ("criterion_id", "verdict", "cause", "action", "reason")}
            receipts = []
            for r in result.get("receipts", ()):
                if (r.get("kind") not in {"read", "reference", "source"} or not r.get("path") or not r.get("sha256")
                        or (r["kind"] in {"read", "reference"} and not r.get("extent", 0))):
                    continue
                ref = {k: r[k] for k in ("kind", "path", "sha256", "url", "observed_bytes_sha256") if k in r}
                if ref not in receipts:
                    receipts.append(ref)
            if receipts:
                finding["receipts"] = receipts
                if version := self.latest.get("candidate_version"):
                    finding["reviewed_version"] = version
            findings.append(finding)
        brief = {"target": self.target, "reviewer": self.reviewer, "candidate_version": self.version,
                 "pending_criteria": [r["criterion_id"] for r in pending], "findings": findings}
        if recheck := facts.pop("peer_recheck", None):
            brief["peer_recheck"] = recheck
        facts["files"] = dict(list(self.files.items())[:32])
        facts["files_total"] = len(self.files)
        if any(rule.reference_path for rule in self.criteria):
            facts["comparison_targets"] = sorted(self.comparison_targets or {p for p, sha in self.files.items() if sha})
        if self.required_source_paths:
            facts["required_source_paths"] = sorted(self.required_source_paths)
        if self.contract:
            # Full source excerpts are retained in describe() and the signed handoff.
            facts["acceptance_contract"] = {"id":self.contract["id"], "registered_version":self.contract["registered_version"]}
        return {**brief, **facts}

    def resume_after_answer(self, human_state):
        if self.state == "awaiting_user" and human_state != self.answer_at:
            self.answered_issues.update((self.version, finding["criterion_id"]) for finding in self.latest["results"]
                                        if finding["cause"] == "business")
            self.state = "review"

    def apply(self, assessment, human_state, *, allow_user_input=True):
        previous = {g["criterion_id"] for g in self.verification_gaps()}
        self.covered_failed_sites.update(self._covered_sites(self.latest))
        new_sites = self._covered_sites(assessment) - self.covered_failed_sites
        self.covered_failed_sites.update(new_sites)
        self.latest = assessment
        required = {r.id for r in self.criteria if r.required}
        gaps = [r for r in assessment["results"] if r["criterion_id"] in required and r["verdict"] != "passed"]
        if not gaps:
            self.state = "passed"
            return
        # IDs of tool calls and prose can change without the candidate or
        # findings changing. Only fresh substantive evidence can reopen work.
        key = (self.version, tuple(sorted((g["criterion_id"], g["verdict"], g["cause"],
                    tuple(sorted(e["fingerprint"] for e in g["receipts"]))) for g in gaps)), human_state,
               frozenset(self.covered_failed_sites))
        if key in self.visited:
            self.state = "blocked"
            return
        self.visited.add(key)
        if any(g["cause"] == "product" for g in gaps):
            self.state = "repair"
        elif any(g["cause"] == "business" for g in gaps):
            questions = {(self.version, g["criterion_id"]) for g in gaps if g["cause"] == "business"}
            if not allow_user_input or questions <= self.answered_issues:
                self.state = "blocked"
                return
            self.state = "awaiting_user"
            self.answer_at = human_state
        else:
            remaining = {g["criterion_id"] for g in gaps}
            # A new receipt ID, another failed attempt or a rewritten summary
            # cannot make progress on a frozen obligation.
            advanced = remaining < previous or (remaining <= previous and new_sites)
            self.state = "blocked" if previous and not advanced else "review"

    def _covered_sites(self, assessment):
        if self.kind != "reviewer_probe" or assessment.get("candidate_version") != self.version:
            return set()
        return {(self.version, g["criterion_id"], receipt["countercheck"]["original_line"])
                for g in assessment.get("results", []) if g.get("cause") == "verification"
                and g.get("verdict") == "inconclusive" and g["criterion_id"].removeprefix("probe_") in self.probes
                for receipt in g["receipts"] if receipt.get("countercheck")}

    def interrupted_repair(self, frozen, receipts):
        """Resume only new product bytes or attributable evidence on a known checkpoint."""
        evidence = frozen["evidence"]
        paths = frozen["required"] or self.files
        product = {path: frozen["manifest"].get(path) for path in paths}
        key = (self.version, digest(product))
        failures = set(frozen["failed_checks"])
        prior = self.interrupted_checkpoints.get(key)
        # Keep the original commands across restores. An identical script exit with
        # a new tool-call ID or a new script unrelated to a failure adds no coverage.
        commands = dict(prior["commands"]) if prior else {}
        for entry in evidence["failed_check_details"]:
            if entry.get("check") and entry["scope_id"] in failures:
                commands[entry["check"]] = entry["scope_id"]
        verified = {ref for ref, row in receipts.items() if ref.startswith("pass:") and row.get("asserted")
                    and any(product.get(path) == sha for path, sha in row.get("asserted_inputs", {}).items())}
        for entry in evidence["script_exits"].values():
            scope = commands.get(entry["check"])
            if scope and entry["exit_code"] == 0 and frozen["manifest"].get(entry["path"]) == entry["sha256"]:
                verified.add("exit:" + scope)
        if prior:
            if not failures < prior["failures"] and not verified - prior["verified"]:
                return "同一冻结返工再次请求到期，交付、未消除的检查及已运行脚本的证据均无变化或未改善。"
            failures &= prior["failures"]
            verified |= prior["verified"]
        self.interrupted_checkpoints[key] = {"failures":failures, "verified":verified, "commands":commands}
        self.visited.add(("provider_deadline", key, frozenset(failures), frozenset(verified)))
        self.state = "repair"
        self.host_issue = ""
        return ""

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
    def __init__(self, required_source_paths=()):
        self.items = {}
        self.candidates = {}
        self.path_candidates = {}
        self.public_versions = {}
        self.required_source_paths = set(required_source_paths)
        self.source_authors = {}

    def record_author(self, role, changes):
        for change in changes:
            path, sha = change["path"], change.get("sha256")
            if path in self.required_source_paths and sha:
                self.source_authors[path, sha] = role

    def missing_source_reviews(self):
        missing = self.required_source_paths.copy()
        for item in self.items.values():
            if item.state != "passed" or item.latest.get("candidate_version") != item.version:
                continue
            for path, sha in item.sourced_files(item.latest["results"]).items():
                if self.public_versions.get(path) == sha and self.source_authors.get((path, sha)) != item.reviewer:
                    missing.discard(path)
        return sorted(missing)

    def next(self, human_state):
        for item in self.items.values():
            item.resume_after_answer(human_state)
            if item.state != "passed":
                return item
        return None

    def published(self, report, manifest, changes):
        self.public_versions = manifest
        self.record_author(report["role"], changes)
        paths = set(report["deliverable_paths"]) | {c["path"] for c in changes}
        self.candidates[report["task_id"]] = {p:manifest.get(p) for p in sorted(paths)}
        # An unchanged file mentioned in somebody else's deliverables must not
        # reassign its authorship. Track the task that actually published these bytes.
        for change in changes:
            path, sha = change["path"], change.get("sha256")
            if sha and manifest.get(path) == sha:
                self.path_candidates[path] = (report["task_id"], sha)
            else:
                self.path_candidates.pop(path, None)
        for item in self.items.values():
            if item.state != "passed":
                continue
            references = {e["path"]: e["sha256"] for finding in item.latest.get("results", [])
                          for e in finding["receipts"] if e["kind"] == "reference"}
            if any(manifest.get(p) != v for p,v in (item.files | references).items()):
                item.files = {p:manifest.get(p) for p in item.files}
                # A reviewer who edits previously approved product bytes cannot
                # independently sign off their own new version.
                item.state = "blocked" if item.reviewer == report["role"] else "review"

    def open(self, raw, reviewer, reports, *, drafts=None, project_version=None, inherited=None):
        spec = ReviewRequest.model_validate(raw)
        ids = [rule.id for rule in spec.criteria]
        if len(set(ids)) != len(ids):
            raise ValueError("每项标准须使用唯一 ID。")
        if active := next((r for r in self.items.values() if r.draft_task and
                           spec.candidate in {r.target,r.author,r.draft_task}), None):
            original_rules = active.criteria[:len(active.criteria) - len(active.probes)]
            if not any(r.required for r in spec.criteria) or (
                    (active.reviewer != reviewer or original_rules != spec.criteria) and not active.verification_gaps()):
                raise ValueError("这个目标已绑定独立验收标准和验收者。")
            return active
        author = next((r for r in reversed(reports) if spec.candidate in {r["task_id"], r["role"]}
                       and not r.get("promoted_candidate")), None)
        if author is None:
            binding = self.path_candidates.get(spec.candidate)
            if binding and self.public_versions.get(spec.candidate) == binding[1]:
                author = next((r for r in reversed(reports) if r["task_id"] == binding[0]), None)
        publication = None
        if author is None and inherited:
            publication = inherited(spec.candidate)
            if publication:
                author = publication
        if not author:
            raise CandidateLookupError("请选择本群其他成员产生的候选。找不到与当前文件匹配的同群发布回执，请核对成稿路径或原任务 ID。")
        if author["role"] == reviewer:
            raise CandidateLookupError("请选择本群其他成员产生的候选。原作者不能独立验收自己的文件。")
        draft_task = None
        objective = author["objective"]
        probes = {}
        probe_products = {}
        if publication:
            inherited_rules = {}
            for raw_rule in publication["unresolved"]:
                rule = Rule.model_validate(raw_rule)
                if rule.id in inherited_rules and inherited_rules[rule.id] != rule:
                    raise ValueError("同一原件已有互相冲突的历史标准定义，无法确定应沿用哪一条：" + rule.id)
                inherited_rules[rule.id] = rule
            conflicts = [r.id for r in spec.criteria if r.id in inherited_rules and r != inherited_rules[r.id]]
            if len(inherited_rules) > 24:
                raise ValueError("该原件的未决必需项超过单次可绑定的 24 项，无法完整重开复核。")
            if conflicts:
                canonical = [rule.model_dump(exclude_none=True) for rule in inherited_rules.values()]
                source_hint = ""
                if self.required_source_paths.intersection(publication["files"]) and not any(
                        r.required and r.method == "source" for r in inherited_rules.values()):
                    source_hint = "本轮用户还指定了来源核查，请在新增项中保留必需的 source 标准。"
                raise InheritedCriteriaConflict(
                    "原件已有未通过的标准，同一 ID 须保留原定义：" + "、".join(conflicts) +
                    "。保持 review.candidate，移除 criteria 中的冲突项，宿主会原样补齐；保留其余新增项。"
                    "若改写内容是额外要求，请用新 ID 追加。只沿用旧项可省略 criteria。" +
                    source_hint + "原定义：" + json.dumps(canonical, ensure_ascii=False))
            spec.criteria.extend(rule for key, rule in inherited_rules.items() if key not in ids)
            if len(spec.criteria) > 24:
                raise ValueError("必需旧项与新增标准合计超过 24 项。请减少新增项，保留全部旧项。")
        if not any(rule.required for rule in spec.criteria):
            raise ValueError("请在 review.criteria 提供至少一项必需标准。省略 criteria 只适用于有未决必需项的同群已发布原件。")
        if publication:
            files = publication["files"]
        elif author["task_id"] in self.candidates:
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
            # An explicit review selects this complete checkpoint. The first
            # candidate and a later repair disclose the same original evidence.
            targets = set(draft.get("required", ()))
            for scope, probe in draft.get("inline_failures", {}).items():
                ref = "probe_" + scope
                if len(spec.criteria) >= 24 or ref in ids:
                    continue  # Its failed scope stays mandatory in the host.
                spec.criteria.append(Rule(id=ref, method="execute", description=(
                    "用独立执行检查复核原检查涉及的业务行为，说明每项原失败来源与所见结果；"
                    "不能仅以作者改写后的 PASS 汇总代替复核。原任务 "
                    + probe["task_id"] + "，工具回执 " + probe["tool_call_id"])))
                probes[scope] = probe
                targets.update(probe.get("input_paths", ()))
                targets.update(probe.get("investigation_paths", ()))
                targets.update(probe.get("program_inputs", {}))
            if probes:
                probe_products = {p: files[p] for p in targets if p in files}
        if not files:
            raise ValueError("该交接没有可绑定版本的工件。")
        published_paths = {p for p, sha in files.items() if sha}
        required_sources = self.required_source_paths & published_paths
        comparison_targets = {spec.candidate} if spec.candidate in published_paths else required_sources or published_paths
        for rule in spec.criteria:
            missing = set(rule.artifact_paths or ()) - published_paths
            if missing:
                raise ValueError(f"标准 {rule.id} 涉及当前候选以外的文件：" + "、".join(sorted(missing)))
            if rule.reference_path:
                if not comparison_targets - {rule.reference_path}:
                    raise ValueError(f"标准 {rule.id} 的 reference_path 须指向成稿以外的本地原始记录。"
                                     "检查成稿字数、哈希或结构时请省略 reference_path；确需对照时，用 review.candidate "
                                     "指定待审文件，并将另一份原始记录填入 reference_path。")
                if self.public_versions and self.public_versions.get(rule.reference_path) is None:
                    raise ValueError(f"公共工作区找不到本地原始记录 {rule.reference_path}，请核对 reference_path 后重试。")
        existing = self.items.get(author["task_id"])
        bound_reviewer = existing.reviewer if existing else reviewer
        if any(self.source_authors.get((p, files[p])) == bound_reviewer for p in required_sources):
            raise ValueError("请另选评审人。指定文件的当前版本由这名成员写入。")
        if existing:
            if publication and existing.files != files:
                raise ValueError("这次复核已绑定其他文件范围，请沿用原目标。")
            if publication and any(rule not in existing.criteria for rule in spec.criteria):
                raise ValueError("当前复核已开始，新增或改写的标准尚未登记。请先完成已绑定的复核，再另行验收新增要求。")
            if (existing.reviewer != reviewer or existing.criteria != spec.criteria) and not existing.verification_gaps():
                raise ValueError("这个目标已绑定独立验收标准和验收者。")
            return existing
        if required_sources and not any(rule.method == "source" and rule.required for rule in spec.criteria):
            raise ValueError("这个候选含用户指定的最终文件，请加入必需的 source 标准：" + "、".join(sorted(required_sources)))
        review = Review(author["task_id"], author["role"], reviewer, objective, spec.criteria, dict(files), draft_task=draft_task, probes=probes,
                        required_source_paths=required_sources, comparison_targets=comparison_targets, probe_product_files=probe_products)
        if publication:
            review.publication = {k:publication[k] for k in ("run_id", "task_id", "role", "files")}
            if publication.get("criteria_corrections"):
                review.publication["criteria_corrections"] = publication["criteria_corrections"]
        return review


def manifest_issue(expected, actual, *, exact=False, secret=""):
    """Return bounded, path-specific differences without exposing file contents."""
    paths = expected.keys() | actual.keys() if exact else expected
    changed = sorted(p for p in paths if actual.get(p) != expected.get(p)
                     or (exact and (p in actual) != (p in expected)))
    if not changed:
        return None
    rows = [f"{snapshot_location(Path(p), secret)} 原定 {expected.get(p) or '缺失'}，实际 {actual.get(p) or '缺失'}"
            for p in changed[:5]]
    if len(changed) > len(rows):
        rows.append(f"另有 {len(changed) - len(rows)} 项不同")
    return "；".join(rows)


class ReviewSession:
    """The member proposes judgments; the host supplies all evidence identifiers."""
    def __init__(self, item, original, progress, attempt_id, *, private_snapshot_issue=None, promotion_issue=None,
                 review_notes=(), review_scripts=None, available_tools=(), editable_reports=(), report_outputs=(), reviewer_task=None):
        self.item = item
        self.execution_actions = {name: instruction for name, instruction in {
            "run_python": "从冻结工件取得实际值，执行顶层 assert actual == expected；通过后用 print('PASS 检查名称') 报告这一项。",
            "run_browser": "运行 Playwright 检查，记录 assert 或 expect 的结果与页面版本。",
            "run_shell": "运行含相关断言的现有测试套件，保留测试框架的逐项结果和真实退出码。",
        }.items() if name in available_tools}
        self.review_notes = set(review_notes)
        self.review_scripts = dict(review_scripts or {})
        self.reference_paths = {r.reference_path for r in item.criteria if r.reference_path}
        self.private_snapshot_issue = private_snapshot_issue
        self.promotion_issue = promotion_issue
        self.promotion_issue_seen = False
        self.original = original
        self.progress = progress
        self.attempt_id = attempt_id
        self.receipts = {}
        self.counterchecks = {}
        self.assessment = None
        self.reviewer_task = reviewer_task
        self.submission_issue = ""
        self.reported_refusals = set()
        self.pending_refusals = []
        self.evidence_handoff = None
        self.source_files = {}
        self.last_snapshot_issue = None
        protected = set(item.files) | self.reference_paths | item.required_source_paths
        for finding in self.retained_findings().values():
            protected.update(r["path"] for r in finding["receipts"] if r.get("path"))
        self.editable_reports = set(editable_reports) - protected
        self.frozen = {path: sha for path, sha in (original | item.files).items() if path not in self.editable_reports}
        self.report_outputs = (self.editable_reports | set(report_outputs)) - protected
        self.execution_history = {}
        self.last_execution_manifest = dict(original)

    def guard_file_tools(self, registry):
        tool = registry.get("run_python")
        if tool:
            registry.add(replace(tool, fn=partial(self._review_python, tool.fn)))
        for name in ("write_file", "edit_file", "append_file", "delete_file"):
            tool = registry.get(name)
            if tool:
                registry.add(replace(tool, fn=partial(self._guard_write, name, tool.fn)))

    def _review_python(self, run, ctx, args):
        if not any(r.method == "execute" and r.artifact_paths for r in self.item.criteria):
            return run(ctx, args)
        # A zero exit can occur before an assertion (e.g. sys.exit(0)).
        # Only a script that reached its last statement can attest its top-level assertions.
        with tempfile.TemporaryDirectory(dir=ctx.sandbox.command_tmpdir) as temp:
            marker = Path(temp) / "completed"
            result = run(replace(ctx, review_completion_file=marker), args)
            if result.ok and marker.is_file():
                result.meta["review_script_completed"] = True
            return result

    def _guard_write(self, name, original_tool, ctx, args):
        path = ctx.sandbox.relative(ctx.sandbox.resolve(args["path"]))
        if path in self.review_scripts:
            return ToolResult.failure("这是独立检查程序。请在单独的评审附件路径编写并执行：" + self.review_scripts[path])
        if path in self.frozen:
            return ToolResult.failure(f"独立评审的冻结文件不可修改 {snapshot_location(Path(path), '')}，"
                                      f"原定 {self.frozen[path] or '缺失'}。请在系统临时目录另建检查脚本，"
                                      "评审说明写入新的或宿主指定的附件路径。")
        if (self.private_snapshot_issue and name != "delete_file"
                and path not in self.review_notes and path not in self.review_scripts.values() and path not in self.source_files):
            parts = Path(path).parts
            # copy_project excludes hidden entries and cache directories from
            # the frozen manifest. Existing tool-authored source receipts are
            # verified by digest at signing; their retrieval tools stay usable.
            if not any(p.startswith(".") for p in parts) and not PROJECT_CACHE_DIRS.intersection(parts[:-1]):
                reports = "可写的正式附件：" + "、".join(sorted(self.review_notes)) + "。" if self.review_notes else ""
                return ToolResult.failure(f"新增 {snapshot_location(Path(path), '')} 会改变私有裁决的冻结快照。"
                    + reports + "请在 submit_review 的 reason 中记录说明；临时探针可用 run_python 配合 tempfile 在项目外运行。")
        return original_tool(ctx, args)

    def snapshot_issue(self):
        manifest = self.progress.project_versions()
        receipts = {}
        if self.assessment:
            receipts = {r["path"]: r["sha256"] for finding in self.assessment["results"] for r in finding["receipts"]
                        if r["kind"] in {"review_note", "source", "reference"} and manifest.get(r["path"]) != r["sha256"]}
        issue = manifest_issue(receipts, manifest)
        if not issue and self.item.peer_recheck.get("candidate_version") == self.item.version:
            issue = manifest_issue(self.item.peer_recheck["reference_files"], manifest)
        if not issue:
            if self.private_snapshot_issue:
                issue = self.private_snapshot_issue()
            else:
                issue = manifest_issue(self.item.files, self.original) or manifest_issue(self.frozen, manifest)
        if issue:
            return (f"评审快照与绑定版本 {self.item.version} 不符。{issue}。停止使用改动后的副本签发结论，"
                    "向负责人交接差异，从原冻结版本重新发起评审。需要修订原件时交回作者。")
        return None

    def unchanged(self):
        self.last_snapshot_issue = self.snapshot_issue()
        return self.last_snapshot_issue is None

    def observe(self, record):
        current = self.progress.review_evidence()
        checks = [r for r in self.receipts.values() if r["kind"] in {"passed_check", "failed_check"}]
        if current or checks:
            manifest = self.progress.project_versions()
            changed = sorted(p for p in manifest.keys() | self.last_execution_manifest.keys()
                             if manifest.get(p) != self.last_execution_manifest.get(p))
            self.last_execution_manifest = manifest
            for old in checks:
                active = current.get(old["legacy_ref"])
                if active and active["tool_call_id"] == old["tool_call_id"]:
                    continue
                if old["kind"] == "passed_check":
                    reason = ("报告已更新" if changed and set(changed) <= self.report_outputs else
                              "工作区输入已变化" if changed else "同一检查已有后续执行")
                    self.execution_history[old["ref"]] = {
                        "ref": old["ref"], "scope": old["scope"], "alias": old["legacy_ref"], "label": old["label"],
                        "tool_call_id": old["tool_call_id"], "receipt_id": old["receipt_id"],
                        "reason": reason, "paths": changed[:16], "changed_count": len(changed)}
                del self.receipts[old["ref"]]
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
            if obs.ok and obs.source in {"read_file", "read_lines"} and meta.get("sha256"):
                row = {"path":path, "sha256":meta["sha256"],
                       "position":meta.get("offset",meta.get("start_line",0)),
                       "extent":meta.get("returned_chars",meta.get("returned_lines",0))}
                if self.item.files.get(path) == row["sha256"]:
                    self._add("file:" + path, {"kind":"read", **row}, obs.tool_call_id)
                if path in self.reference_paths and self.original.get(path) == row["sha256"] and row["extent"] > 0:
                    self._add("reference:" + path, {"kind":"reference", **row}, obs.tool_call_id)

        evidence = self.progress.evidence()
        # Positive and negative test events come from the host's provenance
        # policy. A member printing an opinion cannot mint a check receipt.
        for ref in list(self.receipts):
            if self.receipts[ref]["kind"] in {"review_note", "source", "reference"}:
                note = self.receipts[ref]
                if self.progress.project_versions().get(note["path"]) != note["sha256"]:
                    del self.receipts[ref]
        for ref, event in current.items():
            receipt = self._add(ref, {k:v for k,v in event.items() if k != "tool_call_id"}, event["tool_call_id"])
            if self.item.kind == "reviewer_probe" and (observed := self.progress.observed_python_checks.get(event["tool_call_id"])):
                for rule in self.item.criteria:
                    probe = self.item.probes.get(rule.id.removeprefix("probe_"))
                    if (probe and rule.required and rule.method == "execute" and self._product_assertion(receipt, rule)
                            and (site := corrected_assertion(probe, *observed))):
                        self.counterchecks[(rule.id, receipt["receipt_id"])] = site
        for d in evidence["diagnostic_checks"]:
            row = {"kind":"diagnostic", "tool":d["tool"], "check":d["check"], "reason":d["reason"]}
            self._add("observation:" + digest(row)[:12], row, None)
        # Compare only after observing the complete tool batch. A reviewer can
        # acquire new evidence beside a submission, and must see a refusal on a
        # previous turn before it can constitute an unchanged retry.
        self.evidence_handoff = None
        executed = iter(self.pending_refusals)
        refusals = []
        for obs in record.observations:
            if obs.source != "submit_review":
                continue
            if obs.meta.get("invalid_arguments"):
                issues = [{"message":obs.content}]
                refusals.append(({}, self._refusal_receipt({}, issues)))
            elif obs.meta.get("review_refusal"):
                refusals.append(next(executed))
        if refusals and not self.assessment:
            # Use the final submission in the batch; a corrected proposal in it
            # can supersede an earlier decline even when it reveals a new gap.
            args, receipt = refusals[-1]
            key = receipt["decision_fingerprint"]
            if key in self.reported_refusals and self._refusal_key(args, receipt["issues"]) == key:
                self.evidence_handoff = receipt
        self.reported_refusals.update(row["decision_fingerprint"] for _, row in refusals)
        self.pending_refusals.clear()
        if self.assessment and any(obs.ok and obs.source == "submit_review" for obs in record.observations):
            # Other tools in the same batch can introduce failures after the
            # submission. Close only after the full observed batch agrees.
            if self.assessment["verdict"] == "passed":
                _, self.submission_issue = self.reviewer_failures(self.assessment["results"])
                if self.submission_issue:
                    self.assessment = None
                    return
            self.progress.require_handoff("结构化验收结果已记录")

    def verification_conflict_issue(self, results):
        """Every unresolved required finding must present a checkable conflict on these exact inputs."""
        rules = {r.id:r for r in self.item.criteria if r.required}
        covered_sources = set(self.item.sourced_files(results))
        for finding in results:
            rule = rules.get(finding["criterion_id"])
            if not rule or finding["verdict"] == "passed":
                continue
            if (finding["verdict"], finding["cause"]) != ("inconclusive", "verification"):
                return f"{rule.id} 尚有与核查方法无关的必需缺项。"
            receipts = finding["receipts"]
            reads = {r["path"]: r["sha256"] for r in receipts if r["kind"] == "read" and r.get("extent", 0) > 0
                     and self.item.files.get(r["path"]) == r["sha256"]}
            if rule.reference_path:
                if not reads.keys() & self.item.compare_with(rule.reference_path) or not any(
                        r["kind"] == "reference" and r["path"] == rule.reference_path
                        and self.original.get(r["path"]) == r["sha256"] for r in receipts):
                    return f"{rule.id} 缺少产品与对照原件的同版读取回执。"
            if rule.method == "source":
                if not reads or not any(r["kind"] == "source" for r in receipts):
                    return f"{rule.id} 缺少来源核查或同版读取回执。"
                covered_sources.update(reads)
            if (rule.method == "inspect" and not reads) or not any(r["kind"] == "failed_check" for r in receipts):
                return f"{rule.id} 缺少对应的原失败回执或必要的检查记录。"
            targets = self.item.compare_with(rule.reference_path)
            passes = [r for r in receipts if r["kind"] == "passed_check" and r.get("asserted")
                      and any(path in targets and self.item.files.get(path) == sha
                              for path, sha in r.get("asserted_inputs", {}).items())
                      and (not rule.reference_path or
                           r.get("asserted_inputs", {}).get(rule.reference_path) == self.original[rule.reference_path])]
            if not passes:
                return f"{rule.id} 缺少在冻结产品输入上实际执行的正面断言；仅访问端口、读文件或退出 0 不足以裁决。"
            for failed in receipts:
                if failed["kind"] != "failed_check" or not (documents := failed.get("browser_inputs")):
                    continue
                if not any(path in targets and self.item.files.get(path) == sha
                           and any(r.get("browser_inputs", {}).get(path) == sha for r in passes)
                           for path, sha in documents.items()):
                    return f"{rule.id} 的纠正断言尚未在原失败页面的同一冻结版本上实际执行。"
        missing = self.item.required_source_paths - covered_sources
        return "缺少原始来源核查：" + ", ".join(sorted(missing)) if missing else ""

    def reviewer_failures(self, results, *, include_verification=False):
        """A product judgment may dispute only explicitly cited, transferable failures."""
        if not self.reviewer_task or not (failed := self.progress.failed_checks()):
            return {}, ""
        peer = self.item.peer_recheck
        already = peer.get("adjudicated_scopes", {}) if peer.get("candidate_version") == self.item.version else {}
        repeated = {scope: already[scope] for scope in failed.keys() & already.keys()}
        if repeated:
            return {}, ("原版本的相同失败检查已有独立裁决：" + json.dumps(repeated, ensure_ascii=False) +
                        "。请核对该次裁决与当前结果。真实产品缺陷签 changes_requested/product；"
                        "重复了已判有误的探针则签 inconclusive/verification，在干净的原版本上使用纠正方法独立重测。")
        probes = self.progress.inline_failures()
        cited = {e["receipt_id"] for finding in results if finding["verdict"] == "passed" or
                 (include_verification and finding["verdict"] == "inconclusive" and finding["cause"] == "verification")
                 for e in finding["receipts"] if e["kind"] == "failed_check"}
        details = {r["scope_id"]: r for r in self.progress.evidence()["failed_check_details"]}
        disputes, outstanding = {}, []
        for scope, label in failed.items():
            receipt = self._resolve("fail:" + scope)
            probe = probes.get(scope, {})
            transferable = bool(receipt and probe.get("task_id") == self.reviewer_task)
            if transferable and receipt["receipt_id"] in cited:
                disputes[scope] = probe
            else:
                detail = details.get(scope, {})
                outstanding.append({"scope": scope, "label": label, "ref": receipt and receipt["ref"],
                    "tool": detail.get("tool"), "check": detail.get("check"), "tool_call_id": detail.get("tool_call_id"),
                    "error": (detail.get("error_excerpt") or "")[:700], "available_for_peer": transferable})
        if not outstanding:
            return disputes, ""
        return {}, ("仍有未处理的实跑失败，全通过签署尚未生效。请核实原检查，同一检查复验通过会清除此项。"
            "若你确认是检查方法有误且 available_for_peer 为 true，在对应既有标准的 evidence 同时列出失败 ref 与实际正面证据，"
            "说明分歧，由独立同伴裁决。确有产品缺陷时提交 changes_requested/product；证据不足可提交 inconclusive/verification。"
            "不同脚本产生的 PASS 不能单独核销原失败。实际回执：" + json.dumps(outstanding, ensure_ascii=False))

    def _add(self, ref, row, tool_call_id):
        fingerprint = digest(row)
        receipt_id = digest([self.attempt_id, tool_call_id, row])
        # The same observation reused on a later turn keeps its original identity.
        short = "E" + receipt_id[:16]
        if short in self.receipts and self.receipts[short]["receipt_id"] != receipt_id:
            short = "E" + receipt_id
        self.receipts[short] = {**row, "ref":short, "legacy_ref":ref, "attempt_id":self.attempt_id,
                                "tool_call_id":tool_call_id, "fingerprint":fingerprint,
                                "receipt_id":receipt_id}
        return self.receipts[short]

    def _resolve(self, ref):
        if ref in self.receipts:
            return self.receipts[ref]
        # Older clients used literal URLs and paths. A URL that returned two
        # different records must now be disambiguated by its exact receipt.
        matches = [r for r in self.receipts.values() if r["legacy_ref"] == ref]
        if matches and (len({r["fingerprint"] for r in matches}) == 1 or matches[-1]["kind"] in {"read", "reference"}):
            return matches[-1]
        return None

    def _historical_execution(self, ref):
        old = self.execution_history[ref]
        current = self._resolve(old["alias"])
        return {**old, "current_ref": current["ref"] if current else None}

    def catalog(self, offset=0, limit=12, history_offset=0):
        rows = list(self.receipts.values())
        evidence = [{k:r[k] for k in ("ref", "kind", "path", "url", "label", "check", "sha256",
                                      "position", "extent") if k in r} for r in rows[offset:offset + limit]]
        for row in evidence:
            if row["kind"] in {"passed_check", "failed_check"}:
                entry = self.receipts[row["ref"]]
                if row["kind"] == "passed_check":
                    row["alias"] = entry["legacy_ref"]
                if entry.get("criterion_inputs"):
                    row["criterion_ids"] = sorted(entry["criterion_inputs"])
        history = list(reversed(self.execution_history))
        return {"candidate_version":self.item.version, "offset":offset, "total":len(rows),
                "evidence":evidence, "needs":self.evidence_needs(), **self.execution_guidance(),
                **({"historical_checks":[self._historical_execution(ref) for ref in history[history_offset:history_offset + limit]],
                    "historical_total": len(history), "history_offset": history_offset} if history else {})}

    def ordinary_execution(self, rule):
        return rule.method == "execute" and rule.id.removeprefix("probe_") not in self.item.probes

    def execution_guidance(self):
        if not any(self.ordinary_execution(rule) for rule in self.item.criteria):
            return {}
        guidance = {"evidence": "通过项引用本轮与该标准相符的 passed_check 的 E 短 ID。"
                "文件 SHA、脚本 scope、调用 ID 和正常退出记录各有不同用途；证据不足可提交 inconclusive。",
                "actions": self.execution_actions}
        if self.report_outputs:
            guidance["report_then_verify"] = ("先完成评审报告，写业务观察、检查方法和局限。随后对冻结候选运行最后一轮断言，"
                "用 list_review_evidence 取当前 passed_check 的 E 短 ID，直接 submit_review。E 短 ID 记录在结构化签署中，无需回写报告。")
        if self.execution_history:
            guidance["latest_superseded_check"] = self._historical_execution(next(reversed(self.execution_history)))
        if ("run_python" in self.execution_actions and not self.item.requirements
                and not self.item.probes and self.item.kind == "product"):
            guidance["existing_python_check"] = {
                "when": "原 Python 脚本用业务函数返回 bool，再用条件决定退出码，普通退出还没有 passed_check 时，"
                        "先读冻结脚本，确认可复用的真实、只读检查函数。无需重写长检查脚本。",
                "tool": "run_python",
                "code": "import runpy\ndef checked_call(fn, *args, **kwargs):\n"
                        "    try:\n        return fn(*args, **kwargs)\n"
                        "    except SystemExit as exc:\n"
                        "        raise AssertionError('check exited before returning its result') from exc\n"
                        "checks = checked_call(runpy.run_path, 'checks/verify.py', run_name='__review_probe__')\n"
                        "assert checked_call(checks['check_identity']) is True\n"
                        "assert checked_call(checks['check_behavior']) is True\n"
                        "print('PASS existing identity and behavior checks')\n",
                "binding": "将示例路径、函数名换成已读原脚本中的真实名称，分别调用该标准所需的每个业务检查；"
                           "原脚本及工件仍须保持绑定版本。随后用 list_review_evidence 返回的本轮 E 短 ID 提交。",
                "limits": "不要只断言子进程 exit_code == 0，不要用 assert True 或 PASS 文本替代实际检查。"
                          "函数失败、异常或未执行都不表示通过；没有可复用函数时写较小的独立断言或使用标准测试运行器。"
                          "此方法不会自动核销另一个版本或争议 scope。",
            }
        return {"execute": guidance}

    def evidence_needs(self):
        retained = self.retained_findings()
        kinds = {r["kind"] for r in self.receipts.values()}
        reads = {r["path"] for r in self.receipts.values() if r["kind"] == "read" and r.get("extent", 0) > 0}
        references = {r["path"] for r in self.receipts.values() if r["kind"] == "reference"}
        required_paths = self.item.required_source_paths - self.item.sourced_files(retained.values()).keys()
        needs = {}
        for rule in self.item.criteria:
            if rule.id in retained:
                continue
            missing = []
            if rule.artifact_paths and rule.method != "execute":
                if paths := sorted(set(rule.artifact_paths) - reads):
                    missing.append({"tool": "read_lines", "paths": paths})
            if rule.method == "source":
                if paths := sorted(required_paths - reads):
                    missing.append({"tool":"read_lines", "paths":paths})
                elif not reads:
                    missing.append({"tool":"read_lines"})
                if "source" not in kinds:
                    missing.append({"tool":"http_get"})
            elif self.ordinary_execution(rule) and "passed_check" not in kinds:
                missing.extend({"tool": tool} for tool in self.execution_actions)
            elif rule.method == "execute" and not kinds & {"passed_check", "failed_check"}:
                missing.append({"tool":"run_shell"})
            elif rule.reference_path:
                targets = self.item.compare_with(rule.reference_path)
                if not reads & targets:
                    missing.append({"tool":"read_lines", "paths":sorted(targets)})
                if rule.reference_path not in references:
                    missing.append({"tool":"read_lines", "paths":[rule.reference_path]})
            elif rule.method == "inspect" and not kinds & {"read", "passed_check", "review_note"}:
                missing.append({"tool":"read_lines"})
            if rule.artifact_paths and rule.method == "execute" and not self._eligible_checks(rule):
                missing = [{"tool": tool, "paths": rule.artifact_paths, "assertion_label": rule.id}
                           for tool in self.execution_actions if tool in {"run_python", "run_browser"}]
            if missing:
                needs[rule.id] = missing
            if (self.item.probe_product_files and rule.method == "execute" and
                    (rule.id.startswith("probe_") or self.item.author_requested and rule.id == "independent_recheck")):
                if not any(self._product_assertion(r, rule) for r in self.receipts.values()):
                    references = self.item.probe_reference_inputs.get(rule.id, {})
                    paths = sorted(self.item.probe_product_files.keys() | references.keys())
                    needs[rule.id] = [{"tool": tool, "paths": paths, "receipt_kind": "passed_check"}
                                      for tool in self.execution_actions if tool in {"run_python", "run_browser"}]
        return needs

    def _scoped_assertion(self, row, rule):
        checks = (row.get("criterion_inputs") or {}).get(rule.id, ())
        return any(all(self.item.files.get(path) and observed.get(path) == self.item.files[path]
                       for path in rule.artifact_paths) for observed in checks)

    def _bound_assertion_required(self, rule):
        return rule.method == "execute" and (
            bool(rule.artifact_paths) or self.item.author_requested and rule.id == "independent_recheck"
            or self.item.probe_product_files and rule.id.startswith("probe_"))

    def _eligible_checks(self, rule):
        probe = rule.id.removeprefix("probe_") in self.item.probes
        return [row for row in self.receipts.values() if row["kind"] == "passed_check"
                and (not probe or row.get("asserted"))
                and (not self._bound_assertion_required(rule) or self._product_assertion(row, rule))]

    def _product_assertion(self, receipt, rule):
        if rule.artifact_paths:
            return (receipt.get("kind") == "passed_check" and receipt.get("asserted")
                    and self._scoped_assertion(receipt, rule))
        references = self.item.probe_reference_inputs.get(rule.id, {})
        probe = self.item.probes.get(rule.id.removeprefix("probe_"), {})
        browser_probe = probe.get("tool") == "run_browser"
        target_paths = probe.get("browser_inputs") or probe.get("program_inputs") or probe.get("investigation_paths") or (probe.get("input_paths", ())
                      if self.item.kind == "product" or browser_probe else ())
        products = {p: sha for p, sha in self.item.probe_product_files.items()
                    if not target_paths or p in target_paths}
        # A browser harness can fail before it opens a document. When a
        # page was observed, the peer must exercise that same frozen product.
        browser_required = browser_probe and (target_paths or self.item.author_requested)
        inputs = receipt.get("browser_inputs" if browser_required else "asserted_inputs", {})
        # If no product input was observed (e.g. an assertion on constants),
        # a peer's direct test-suite run on the frozen
        # candidate remains admissible; a bare exit or constant assertion is not.
        independent_suite = (self.item.kind == "product" and probe.get("tool") == "run_python"
                             and not target_paths and receipt.get("test_runner"))
        return (receipt.get("kind") == "passed_check" and receipt.get("asserted")
                and references.items() <= receipt.get("asserted_inputs", {}).items()
                and (bool(independent_suite) or any(path not in references and products.get(path) == sha
                                                   for path, sha in inputs.items())))

    def guidance(self, *, show_examples=True):
        # Avoid repeatedly projecting every URL or duplicating the raw records.
        # The paginated tool returns the original, unabridged source mapped to each ref.
        by_kind = {}
        for receipt in self.receipts.values():
            label = receipt.get("url") or receipt.get("path") or receipt.get("label") or receipt.get("check") or ""
            by_kind.setdefault(receipt["kind"], []).append([receipt["ref"], label[:110]])
        summary = {"recent_evidence":{k:v[-6:] for k,v in by_kind.items()},
                   "needs":self.evidence_needs(), "catalog":"list_review_evidence"}
        if show_examples:
            summary.update(self.execution_guidance())
        if self.last_snapshot_issue:
            summary["snapshot_issue"] = self.last_snapshot_issue
        if self.submission_issue:
            summary["submission_issue"] = self.submission_issue
        return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))

    def closing_options(self):
        needs = self.evidence_needs()
        tools = {"submit_review"}
        if self.item.requirements and not self.item.contract:
            tools.update({"register_review_criteria", "read_file", "read_lines", "list_review_evidence"})
        if len(self.receipts) > 6 or self.item.kind == "reviewer_probe" and self.receipts:
            tools.add("list_review_evidence")
        for steps in needs.values():
            tools.update(step["tool"] for step in steps)
        return frozenset(tools)

    def criteria_tool(self):
        from .studio_review_contract import Registration, register
        return Tool("register_review_criteria", "登记本次独立评审的条款。",
                    _review_tool_schema(Registration, "criteria"), partial(register, self))

    def catalog_tool(self):
        def list_evidence(ctx, args):
            offset = args.get("offset", 0)
            history_offset = args.get("history_offset", 0)
            if any(type(n) is not int or n < 0 for n in (offset, history_offset)):
                return ToolResult.failure("offset 和 history_offset 须为非负整数。")
            return ToolResult.success(json.dumps(self.catalog(offset, history_offset=history_offset), ensure_ascii=False))
        return Tool("list_review_evidence", "分页列出本次独立评审实际观察到的短回执及其原始路径、URL、SHA 和缺失的取证步骤。",
                    {"type":"object","properties":{"offset":{"type":"integer","minimum":0}, "history_offset":{"type":"integer","minimum":0}},
                     "additionalProperties":False}, list_evidence)

    def retained_findings(self):
        if not self.item.verification_gaps():
            return {}
        manifest = self.progress.project_versions()
        # Use only authenticated findings on the identical candidate. A
        # source report or review note that disappeared requires a fresh check.
        fresh = self.item.peer_recheck.get("required_fresh_criteria", ()) if self.item.peer_recheck.get("candidate_version") == self.item.version else ()
        return {g["criterion_id"]: g for g in self.item.latest["results"]
                if g["verdict"] == "passed" and g["criterion_id"] not in fresh and all(manifest.get(e["path"]) == e["sha256"]
                    for e in g["receipts"] if e["kind"] in {"read", "source", "review_note", "reference"})}

    def context(self):
        parts = ["本轮独立评审绑定以下版本。submit_review 的证据 ID 来自本次评审的 list_review_evidence。"]
        if retained := self.retained_findings():
            parts.append("同一版本已保留的结论：" + json.dumps(list(retained), ensure_ascii=False))
        if any(r.method == "source" for r in self.item.criteria):
            parts.append("source 项关联本轮候选文件的 read 回执和本轮 http_get 的 source 回执。")
        if any(r.reference_path for r in self.item.criteria):
            parts.append("含 reference_path 的项同时关联候选文件的 read 回执和指定原件的 reference 回执。")
        parts.append("冻结输入为只读。临时脚本可写入系统临时目录。")
        if self.editable_reports:
            parts.append("本轮可编辑的已发布评审报告：" + "、".join(sorted(self.editable_reports)))
        if self.review_notes:
            parts.append("本轮指派的评审附件（回执别名 note:文件名）：" + "、".join(sorted(self.review_notes)))
        parts.append("材料：" + json.dumps(self.item.briefing(), ensure_ascii=False))
        if self.item.probes:
            parts.append("原检查的代码和失败回执，仅供复核：" + json.dumps(self.item.probes, ensure_ascii=False))
        if self.item.kind == "reviewer_probe":
            parts.append("本次裁决范围为原评审引用的争议检查。原评审者的后续回执仅供对照：" +
                         json.dumps(self.item.probe_support, ensure_ascii=False))
        elif self.item.probe_product_files:
            parts.append("作者交接的冻结文件及其检查记录，仅供独立复核：" + json.dumps(
                {"files": self.item.probe_product_files, "support": self.item.probe_support}, ensure_ascii=False))
        if self.item.draft_task:
            parts.append("候选是作者的冻结私有版本，通过签署后由宿主发布。")
        return "\n".join(parts)

    def tool(self):
        return Tool("submit_review", "对本次群内候选版本逐项提交独立验收结论与宿主证据。", SUBMIT_SCHEMA, self.submit)

    def _rejection(self, issues):
        content = json.dumps(issues, ensure_ascii=False)
        return ToolResult.failure(content + "\n当前回执和可执行的取证步骤：" + self.guidance(), issues=issues)

    def _finding(self, rule, result):
        receipts = [self._resolve(ref) for ref in result.evidence]
        unknown = [ref for ref, row in zip(result.evidence, receipts) if row is None]
        if unknown:
            expired = [self._historical_execution(ref) for ref in unknown if ref in self.execution_history]
            if expired:
                raise UnresolvedReviewEvidence("这些 E 引用是历史执行回执：" + json.dumps(expired, ensure_ascii=False)
                    + "。先定稿报告；若已有 current_ref，请核对后引用它或当前唯一 alias。否则在当前版本复跑原检查，"
                      "用 list_review_evidence 获取新 E 后直接签署，无需把 E 写回报告。")
            current = [r["ref"] for r in self.receipts.values()
                       if self.item.kind == "reviewer_probe" and rule.method == "execute" and self._product_assertion(r, rule)]
            hint = "。本标准当前的产品断言回执 " + current[-1] + "，请核对是否适用" if current else ""
            raise UnresolvedReviewEvidence("引用了未观察到或不唯一的证据：" + "、".join(r[:160] for r in unknown)
                             + hint + "。从 list_review_evidence 选择相符的短 ID。")
        kinds = {e["kind"] for e in receipts}
        if rule.artifact_paths and (result.verdict == "passed" or result.cause == "product"):
            if rule.method == "execute":
                expected_kind = "passed_check" if result.verdict == "passed" else "failed_check"
                if not any(e["kind"] == expected_kind and self._scoped_assertion(e, rule) for e in receipts):
                    raise ValueError(f"标准 {rule.id} 缺少与该标准 ID 和冻结文件匹配的 {expected_kind} 回执。")
            else:
                reads = {e["path"] for e in receipts if e["kind"] == "read" and e.get("extent", 0) > 0
                         and self.item.files.get(e["path"]) == e.get("sha256")}
                if missing := set(rule.artifact_paths) - reads:
                    raise ValueError(f"标准 {rule.id} 尚未引用所需冻结文件的读取回执：" + "、".join(sorted(missing)))
        if rule.reference_path and (result.verdict == "passed" or result.cause == "product"):
            if not (any(e["kind"] == "reference" and e["path"] == rule.reference_path for e in receipts)
                    and any(e["kind"] == "read" and e["path"] in self.item.compare_with(rule.reference_path) and e.get("extent", 0) > 0
                            for e in receipts)):
                raise ValueError(f"本地对照须读取并同时引用候选成稿的 read 回执和 {rule.reference_path} 的 reference 回执。")
        if self.item.kind == "reviewer_probe" and result.cause == "product" and result.verdict == "inconclusive":
            raise ValueError("争议尚未查实时请用 inconclusive/verification；确认产品缺陷才用 changes_requested/product。")
        if rule.method == "source" and (result.verdict == "passed" or result.cause == "product"):
            if not {"read", "source"} <= kinds:
                raise ValueError("核实外部事实需要引用所读候选及本次亲自打开的来源（http_get 网页回执）。"
                                 "仅有本地材料时可标记 inconclusive 并向负责人说明所需取证方法。")
            if (self.item.kind == "reviewer_probe" and not any(r["kind"] == "read"
                    and r.get("extent", 0) > 0 and self.item.probe_product_files.get(r["path"]) == r["sha256"]
                    for r in receipts)):
                raise ValueError("原来源争议必须引用同候选的产品事实，不能以评审附件或其他文件代替成稿。")
            if any(self.progress.project_versions().get(e["path"]) != e["sha256"] for e in receipts if e["kind"] == "source"):
                raise ValueError("获取的来源记录已变化，请重新核实。")
        if result.verdict == "passed":
            if self.item.author_requested and rule.id == "independent_recheck":
                if rule.method == "execute" and not any(self._product_assertion(r, rule) for r in receipts):
                    raise ValueError("独立复核需要本轮实际执行冻结交付物的断言回执。")
                if rule.method == "inspect" and not any(r["kind"] == "read" and r.get("extent", 0) > 0
                        and self.item.probe_product_files.get(r.get("path")) == r.get("sha256") for r in receipts):
                    raise ValueError("独立复核需要本轮读取冻结交付物的回执。")
            if (rule.id.removeprefix("probe_") in self.item.probes and rule.method == "execute" and
                    not any(r["kind"] == "passed_check" and r.get("asserted") for r in receipts)):
                raise ValueError("原临时探针的独立复核需要实际断言结果，进程正常退出只能证明运行完成。")
            if (self.item.probe_product_files and rule.id.startswith("probe_") and rule.method == "execute"
                    and not any(self._product_assertion(r, rule) for r in receipts)):
                references = self.item.probe_reference_inputs.get(rule.id, {})
                hint = "，同时取值于绑定的对照原件 " + "、".join(references) if references else ""
                raise ValueError("原失败的裁决必须执行依赖冻结产品实际内容的断言" + hint + "；空断言或其他检查的 PASS 不足以核销该 scope。")
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
        receipts = [{**r, "countercheck": site} if (site := self.counterchecks.get((rule.id, r["receipt_id"]))) else r
                    for r in receipts]
        return {**result.model_dump(), "check_result":check.model_dump(), "receipts":receipts}

    def _refusal_key(self, args, issues):
        # Receipt fingerprints describe what was observed, independent of the
        # call ID. A rewritten reason or a made-up evidence ID is no new fact.
        criteria = {r.id: r for r in self.item.criteria}
        claims = []
        for result in args["results"] if isinstance(args.get("results"), list) else ():
            if not isinstance(result, dict):
                continue
            rule = criteria.get(str(result.get("criterion_id")))
            refs = result.get("evidence")
            refs = refs if isinstance(refs, list) else []
            cited = {row["fingerprint"] for ref in refs if isinstance(ref, str) and (row := self._resolve(ref))}
            claims.append([rule.id if rule else None, result.get("verdict"), result.get("cause"),
                           bool(result.get("action")), sorted(cited)])
        # Relevant newly available evidence must reach the reviewer even if it
        # mistakenly reused an old reference in this very submission.
        available = {}
        affected = {issue.get("criterion_id") for issue in issues}
        for rule in self.item.criteria:
            if rule.id not in affected:
                continue
            if rule.method == "execute":
                rows = self._eligible_checks(rule)
            else:
                kinds = {"read", "reference", "source"} if rule.method == "source" else {"read", "reference", "review_note"}
                rows = [r for r in self.receipts.values() if r["kind"] in kinds]
            available[rule.id] = sorted({r["fingerprint"] for r in rows})
        problems = [issue.get("code") or issue["message"] for issue in issues]
        return digest([self.item.version, args.get("candidate_version") == self.item.version,
                       sorted(claims, key=digest), sorted(problems), available])

    def _refusal_receipt(self, args, issues):
        return {"source":"review_evidence_required", "target":self.item.target,
                "candidate_version":self.item.version, "issues":issues,
                "decision_fingerprint":self._refusal_key(args, issues)}

    def submit(self, ctx, args):
        result = self._validate_submission(args)
        if result.ok or self.assessment:
            return result
        issues = result.meta.get("issues") or [{"message":result.content}]
        receipt = self._refusal_receipt(args, issues)
        result.meta["review_refusal"] = receipt
        self.pending_refusals.append((args, receipt))
        if receipt["decision_fingerprint"] in self.reported_refusals:
            result.content = "该版本的这组证据已被拒签。相同缺项交回负责人，候选保持未签署。\n" + result.content
        return result

    def _validate_submission(self, args):
        try:
            if self.item.requirements and not self.item.contract:
                raise ValueError("本版本尚未登记复核条款（register_review_criteria）。")
            request = Submission.model_validate(args)
            if self.assessment:
                raise ValueError("本次验收已提交。")
            if request.candidate_version != self.item.version:
                raise ValueError(f"提交的候选版本已改变，请核对本次绑定版本 {self.item.version}。")
            issue = self.snapshot_issue()
            if issue:
                raise ValueError(issue)
            rules = {r.id:r for r in self.item.criteria}
            retained = self.retained_findings()
            submitted = [r.criterion_id for r in request.results]
            if len(set(submitted)) != len(submitted) or set(submitted) | retained.keys() != rules.keys():
                raise ValueError("每个标准都须正好有一个结论。")
            results = [finding for cid, finding in retained.items() if cid not in submitted]
            issues = []
            for result in request.results:
                rule = rules[result.criterion_id]
                try:
                    results.append(self._finding(rule, result))
                except ValueError as exc:
                    issue = {"criterion_id":rule.id, "method":rule.method, "message":str(exc)}
                    if self.item.kind == "reviewer_probe" and rule.method == "execute":
                        if result.verdict == "passed" and (result.cause or result.action):
                            issue["verdict_fields"] = "若维持 passed，省略 cause 和 action。"
                        if open_checks := self.progress.failed_checks():
                            issue["pending"] = (f"本轮裁决仍有 {len(open_checks)} 项失败检查，"
                                "请用 list_review_evidence 核对失败与纠正回执，再决定逐项结论。")
                    if isinstance(exc, UnresolvedReviewEvidence):
                        issue["code"] = "unknown_or_superseded_evidence:" + rule.id
                    if result.verdict == "passed" and rule.method == "execute":
                        issue["passed_checks"] = [{"ref":r["ref"], "label":r.get("label",r["legacy_ref"])} for r in self._eligible_checks(rule)][-6:]
                        if not issue["passed_checks"]:
                            issue["missing"] = "尚无可引用的 passed_check。脚本退出 0 或打印布尔值不能单独证明该项通过。"
                            issue["available_tools"] = list(self.execution_actions)
                        if self._bound_assertion_required(rule):
                            if issue["passed_checks"]:
                                issue["next_action"] = "核实上列已绑定冻结产品的回执，并在该项 evidence 中引用对应的 ref。"
                            else:
                                issue["missing"] = "尚无可关联到冻结文件版本的产品断言。"
                                issue["next_action"] = self.evidence_needs().get(rule.id, [])
                    if "next_action" not in issue and (steps := self.evidence_needs().get(rule.id)):
                        issue["next_action"] = steps
                    issues.append(issue)
            if issues:
                return self._rejection(issues)
            by_id = {r["criterion_id"]: r for r in results}
            results = [by_id[rule.id] for rule in self.item.criteria]
            required = {r.id for r in self.item.criteria if r.required}
            decisions = {r["verdict"] for r in results if r["criterion_id"] in required}
            verdict = "changes_requested" if "changes_requested" in decisions else (
                "inconclusive" if "inconclusive" in decisions else "passed")
            if verdict == "passed" and self.item.required_source_paths:
                missing = self.item.required_source_paths - self.item.sourced_files(results).keys()
                if missing:
                    return self._rejection([{"criterion_id":r.id, "method":r.method,
                        "message":"请在必需的 source 判断中，引用实际读过的成稿和本次打开的原始来源；"
                                  "用 read_lines 读取并引用成稿：" + "、".join(sorted(missing))}
                        for r in self.item.criteria if r.required and r.method == "source"])
            if verdict == "passed" and self.reviewer_task:
                _, self.submission_issue = self.reviewer_failures(results)
                if self.submission_issue:
                    raise ValueError(self.submission_issue)
            if verdict == "passed" and self.promotion_issue:
                if not any(r["criterion_id"] in required and any(e["kind"] in {"read","passed_check"} for e in r["receipts"])
                           for r in results):
                    raise ValueError("通过候选需要对作者原件的独立检查，评审者的报告是单独的附件。")
                problem = self.promotion_issue()
                if problem:
                    self.promotion_issue_seen = True
                    self.submission_issue = problem
                    raise ValueError(problem)
            if verdict == "passed" and self.reviewer_task and (missing_paths := self.progress.pending_paths()):
                self.submission_issue = "请先交付本次分工指定的附件，再提交全通过签署：" + "、".join(sorted(missing_paths))
                raise ValueError(self.submission_issue)
            self.submission_issue = ""
            self.assessment = {"target":self.item.target,"candidate_version":self.item.version,
                               "evaluator":self.item.reviewer,"attempt_id":self.attempt_id,
                               "verdict":verdict,"results":results}
            return ToolResult.success("验收已记录：" + verdict)
        except ValidationError as exc:
            problems = [{"message": ".".join(map(str, error["loc"])) + ": " + error["msg"],
                         "code": ".".join(map(str, error["loc"])) + ":" + error["type"]}
                        for error in exc.errors(include_input=False, include_url=False)]
            return self._rejection(problems)
        except (ValueError, OSError) as exc:
            return ToolResult.failure(str(exc))
