"""Register independent review criteria against the actual request and frozen artifacts."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .acceptance import _read_regular
from .schemas import ToolResult


class GroundedCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1200)
    method: Literal["inspect", "execute", "source"]
    artifact_paths: list[str] = Field(min_length=1, description="条款对应的冻结文件相对路径。")
    user_quote: str = Field(min_length=1, max_length=1200, description="本轮用户要求中的原文。")
    basis_path: str | None = Field(default=None, min_length=1, max_length=500, description="依据文件的相对路径。")
    basis_quote: str | None = Field(default=None, min_length=1, max_length=1200, description="依据文件中的原文。")
    reference_path: str | None = Field(default=None, description="inspect 条款的可选参照文件相对路径。")


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    criteria: list[GroundedCriterion] = Field(min_length=1, max_length=24)


def _contains_quote(text, quote):
    return " ".join(quote.split()) in " ".join(text.split())


def register(session, ctx, args):
    from .studio_reviews import Rule, digest

    item = session.item
    try:
        request = Registration.model_validate(args)
        if not item.requirements or item.state != "review" or item.kind != "product":
            raise ValueError("当前任务没有待登记的独立文档验收。")
        if item.version != request.candidate_version:
            raise ValueError("候选版本已改变，请使用当前冻结版本 " + item.version)
        definitions = [c.model_dump(exclude_none=True) for c in request.criteria]
        original_contract = item.contract
        if original_contract:
            if definitions == original_contract["definitions"]:
                return ToolResult.success("已沿用原验收条款。", contract_id=original_contract["id"])
            raise ValueError("该目标已有正式验收条款。返工和复核须沿用原有条款。")
        # A pre-existing execution obligation still applies when the reviewer
        # chooses inspection for all of their own criteria.
        has_execution = any(c.method == "execute" for c in request.criteria)
        host_rules = [rule for rule in item.criteria if rule.id != "independent_recheck"
                      or (rule.method == "execute" and not has_execution)]
        used = {rule.id for rule in host_rules} | {"independent_recheck"}
        if len(definitions) + len(host_rules) > 24:
            raise ValueError("登记条款与宿主已有标准的总数超过评审接口容量。")
        rules, covered, texts = [], set(), {}
        reads = {(r.get("path"), r.get("sha256")) for r in session.receipts.values()
                 if r.get("kind") == "read" and r.get("extent", 0) > 0}
        for spec in request.criteria:
            if spec.id in used or spec.id.startswith("probe_"):
                raise ValueError("每条独立标准须使用唯一、非保留 ID：" + spec.id)
            used.add(spec.id)
            if not _contains_quote(item.requirements, spec.user_quote):
                raise ValueError(f"{spec.id} 引用的用户原文与本群本轮任务不符。")
            paths = set(spec.artifact_paths)
            if any(not item.files.get(path) for path in paths | ({spec.reference_path} if spec.reference_path else set())):
                raise ValueError(f"{spec.id} 包含未冻结或不存在的文件。")
            if bool(spec.basis_path) != bool(spec.basis_quote):
                raise ValueError(f"{spec.id} 须同时提交依据文件路径和原文。")
            if spec.basis_path:
                name = spec.basis_path
                sha = item.files.get(name)
                if not sha or (name, sha) not in reads:
                    raise ValueError(f"缺少本版本依据文件的读取回执：{name}。")
                if name not in texts:
                    raw = _read_regular(ctx.sandbox.root, name)
                    if raw is None or hashlib.sha256(raw).hexdigest() != sha:
                        raise ValueError(f"验收依据 {name} 与冻结版本不一致。")
                    texts[name] = raw.decode("utf-8-sig")
                if not _contains_quote(texts[name], spec.basis_quote):
                    raise ValueError(f"{spec.id} 的依据原文未出现在 {name} 当前冻结版本。")
            rules.append(Rule(id=spec.id, description=spec.description, method=spec.method,
                              reference_path=spec.reference_path, artifact_paths=spec.artifact_paths))
            covered.update(paths)
        missing = set(item.delivery_paths) - covered
        if missing:
            raise ValueError("这些分工交付尚无对应标准：" + "、".join(sorted(missing)))
        contract_id = digest({"target":item.target, "user":item.requirements, "criteria":definitions})
        item.contract = {"id":contract_id, "registered_version":item.version, "definitions":definitions,
                         "basis_versions":{name:item.files[name] for name in texts}}
        item.criteria = [*rules, *host_rules]
        session.reference_paths.update(rule.reference_path for rule in rules if rule.reference_path)
        return ToolResult.success(json.dumps({"contract_id":contract_id, "criteria":[r.id for r in item.criteria]},
                                             ensure_ascii=False), contract_id=contract_id)
    except (OSError, ValueError) as error:
        return ToolResult.failure(str(error))
