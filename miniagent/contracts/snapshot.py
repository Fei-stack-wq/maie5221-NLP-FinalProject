"""Cross-record validation of a complete, supplied contract snapshot.

The catalog is caller-supplied evidence, not an authenticated authority. These
checks neither contact external systems nor enforce live authorization. Historic
checkpoints/reports remain historic; they are not compared to today's resource
contents as if they described the current environment.
"""
from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter, model_validator

from .acceptance import AcceptanceReport, AcceptanceSpec, validate_report
from .artifacts import ArtifactGrant, ArtifactRecord, TaskCreatorRef, validate_artifact_content
from .base import (
    ActorRef, Array, ArtifactRef, ContractModel, Id, JsonObject, Timestamp, VersionRef,
    require, time_value, unique,
)
from .budget import BudgetLease, BudgetScope
from .human import ActionRecord, HumanReply, HumanRequest, validate_approval, validate_reply
from .tasks import AttemptRecord, Checkpoint, RunRecord, TaskRecord

TASK_CLOSED = {"completed", "unverified", "failed", "cancelled"}
ATTEMPT_CLOSED = {"succeeded", "unverified", "failed", "cancelled"}
_TIMESTAMP = TypeAdapter(Timestamp)


def _time(value: Any) -> Any:
    return time_value(_TIMESTAMP.validate_python(value))


class DeliveryReceipt(ContractModel):
    delivery_key: Id
    child_task_id: Id
    parent_task_id: Id
    attempt_id: Id
    delivered_at: Timestamp
    report_id: Id


class ContractSnapshot(ContractModel):
    catalog: JsonObject
    artifact_contents_utf8: JsonObject = {}
    tasks: Array[TaskRecord] = ()
    attempts: Array[AttemptRecord] = ()
    runs: Array[RunRecord] = ()
    acceptance_specs: Array[AcceptanceSpec] = ()
    acceptance_reports: Array[AcceptanceReport] = ()
    budget_scopes: Array[BudgetScope] = ()
    budget_leases: Array[BudgetLease] = ()
    artifacts: Array[ArtifactRecord] = ()
    artifact_grants: Array[ArtifactGrant] = ()
    human_requests: Array[HumanRequest] = ()
    human_replies: Array[HumanReply] = ()
    actions: Array[ActionRecord] = ()
    checkpoints: Array[Checkpoint] = ()
    delivery_receipts: Array[DeliveryReceipt] = ()

    @model_validator(mode="after")
    def cross_record_rules(self) -> ContractSnapshot:
        _Validation(self).validate()
        return self

    def validate_artifact_access(
        self, *, subject: ActorRef, task_id: str, artifact_ref: ArtifactRef,
        operation: str, at: str,
    ) -> None:
        """Check an exact grant in this supplied snapshot, not live permissions."""
        v = _Validation(self)
        task = v.get("tasks", task_id)
        artifact = v.artifact(artifact_ref, task)
        v.actor(subject)
        require(operation in {"read", "materialize"}, "unsupported artifact operation")
        now = time_value(at)
        require(now >= time_value(artifact.created_at), "artifact does not yet exist")
        require(any(
            grant.subject == subject and grant.status == "active"
            and grant.project_id == task.project_id and task_id in grant.task_ids
            and artifact.artifact_id in grant.artifact_ids and operation in grant.operations
            and time_value(grant.created_at) <= now
            and (grant.expires_at is None or now < time_value(grant.expires_at))
            for grant in self.artifact_grants
        ), "permission_denied: no matching active artifact grant")


class _Validation:
    KEYS = {
        "tasks": "task_id", "attempts": "attempt_id", "runs": "run_id",
        "acceptance_reports": "report_id", "budget_scopes": "scope_id",
        "budget_leases": "lease_id", "artifacts": "artifact_id",
        "artifact_grants": "grant_id", "human_requests": "request_id",
        "human_replies": "reply_id", "actions": "action_id", "checkpoints": "checkpoint_id",
    }

    def __init__(self, snapshot: ContractSnapshot) -> None:
        self.s = snapshot
        self.index: dict[str, dict[Any, Any]] = {}
        claimed = set(snapshot.catalog)
        for collection, key in self.KEYS.items():
            records = getattr(snapshot, collection)
            unique(records, collection, key=lambda x: getattr(x, key))
            self.index[collection] = {getattr(x, key): x for x in records}
            ids = set(self.index[collection])
            require(not claimed & ids, f"duplicate identity across record types: {collection}")
            claimed |= ids
        unique(snapshot.acceptance_specs, "spec versions", key=lambda x: (x.spec_id, x.version))
        spec_ids = {x.spec_id for x in snapshot.acceptance_specs}
        require(not claimed & spec_ids, "spec identity collides with another record type")
        self.specs = {(x.spec_id, x.version): x for x in snapshot.acceptance_specs}
        unique(snapshot.delivery_receipts, "delivery_receipts", key=lambda x: x.delivery_key)

    def get(self, collection: str, identity: str) -> Any:
        require(identity in self.index[collection], f"invalid_reference: {collection}/{identity}")
        return self.index[collection][identity]

    def catalog(self, identity: str, kinds: set[str] | None = None) -> dict:
        item = self.s.catalog.get(identity)
        require(isinstance(item, dict), f"invalid_reference: catalog/{identity}")
        if kinds is not None:
            require(item.get("kind") in kinds, f"wrong catalog type for {identity}")
        return item

    def version(self, value: Any, kinds: set[str] | None = None) -> dict:
        item = self.catalog(value.id, kinds)
        versions = item.get("versions", (item.get("version"),))
        require(isinstance(versions, (tuple, list)) and bool(versions)
                and all(type(v) is int and v > 0 for v in versions), "catalog versions must be positive integers")
        require(value.version in versions, f"invalid_reference: version {value.id}/{value.version}")
        return item

    def authority(self, identity: str, project_id: str, *, spec: bool = False) -> None:
        kinds = {"authority", "policy", "authorization_decision"}
        if spec:
            kinds.add("user_message")
        item = self.catalog(identity, kinds)
        require(item.get("project_id", project_id) == project_id, "authority crosses project")

    def resource_environment(self, identity: str, project_id: str) -> str:
        resource = self.catalog(identity, {"resource"})
        require(resource.get("project_id") == project_id, "resource crosses project")
        project = self.catalog(project_id, {"project"})
        environment = resource.get("environment_id", project.get("environment_id"))
        require(type(environment) is str and bool(environment), "resource has no environment identity")
        return environment

    def actor(self, value: ActorRef) -> None:
        kinds = {"profile", "builtin_profile"} if value.kind == "profile" else {value.kind}
        self.catalog(value.id, kinds)

    def spec(self, reference: Any) -> AcceptanceSpec | None:
        if reference is None:
            return None
        key = (reference.id, reference.version)
        require(key in self.specs, f"invalid_reference: spec {key}")
        return self.specs[key]

    def same_task(self, record: Any, task_id: str, attempt_id: str | None = None) -> None:
        require(record.task_id == task_id, "record belongs to a different task")
        if attempt_id is not None:
            require(record.attempt_id == attempt_id, "record belongs to a different attempt")

    def same_scope(self, task: TaskRecord, scope: Any) -> None:
        require(task.project_id == scope.project_id and task.root_id == scope.root_task_id,
                "record belongs to a different project/root scope")

    def artifact(self, reference: ArtifactRef, task: TaskRecord | None = None) -> ArtifactRecord:
        value = self.get("artifacts", reference.artifact_id)
        require(value.content_hash == reference.content_hash, "artifact hash reference mismatch")
        if task is not None:
            self.same_scope(task, value.scope)
        return value

    def evidence(self, ref: Any, task: TaskRecord | None = None) -> None:
        if ref.kind == "artifact":
            value = self.get("artifacts", ref.id)
            if task is not None:
                self.same_scope(task, value.scope)
        elif ref.kind == "reply":
            reply = self.get("human_replies", ref.id)
            request = self.get("human_requests", reply.request_id)
            if task is not None:
                other = self.get("tasks", request.task_id)
                require((other.project_id, other.root_id) == (task.project_id, task.root_id), "reply evidence crosses project/root")
        elif ref.kind == "receipt":
            self.catalog(ref.id, {"tool_receipt", "receipt", "reconciliation_receipt"})
        else:
            self.catalog(ref.id)

    def acyclic(self, collection: str, edges: Any) -> None:
        graph = {identity: tuple(edges(value)) for identity, value in self.index[collection].items()}
        visited: set[str] = set()
        active: set[str] = set()
        for start in graph:
            stack = [(start, False)]
            while stack:
                node, exiting = stack.pop()
                require(node in graph, f"invalid_reference: {collection}/{node}")
                if exiting:
                    active.remove(node)
                    visited.add(node)
                elif node not in visited:
                    require(node not in active, f"cycle in {collection}")
                    active.add(node)
                    stack.append((node, True))
                    stack.extend((child, False) for child in graph[node])

    def descendants(self, task_id: str) -> set[str]:
        result = {task_id}
        pending = [task_id]
        while pending:
            parent = pending.pop()
            for task in self.s.tasks:
                if task.parent_id == parent and task.task_id not in result:
                    result.add(task.task_id)
                    pending.append(task.task_id)
        return result

    def validate(self) -> None:
        unique(self.s.tasks, "creation_key", key=lambda x: x.creation_key)
        unique(self.s.attempts, "attempt numbers", key=lambda x: (x.task_id, x.number))
        unique(self.s.budget_leases, "call_id", key=lambda x: x.call_id)
        self.acyclic("tasks", lambda x: x.deps)
        self.acyclic("tasks", lambda x: () if x.parent_id is None else (x.parent_id,))
        self.acyclic("tasks", lambda x: tuple(x.deps) + tuple(t.task_id for t in self.s.tasks if t.parent_id == x.task_id))
        self.acyclic("budget_scopes", lambda x: () if x.parent_scope_id is None else (x.parent_scope_id,))
        self.acyclic("artifacts", lambda x: (r.artifact_id for r in x.input_refs))
        self.validate_tasks()
        self.validate_execution()
        self.validate_acceptance()
        self.validate_artifacts()
        self.validate_human_actions()
        self.validate_budgets()
        self.validate_checkpoints()

    def validate_tasks(self) -> None:
        for task in self.s.tasks:
            self.catalog(task.project_id, {"project"})
            self.version(task.constraints_ref, {"constraints"})
            root = self.get("tasks", task.root_id)
            require(root.parent_id is None and root.project_id == task.project_id, "invalid task root/project")
            if task.parent_id is not None:
                parent = self.get("tasks", task.parent_id)
                require(parent.root_id == task.root_id and parent.project_id == task.project_id, "parent crosses root/project")
            for name in ("owner_profile_id", "assignee_profile_id"):
                if getattr(task, name) is not None:
                    self.actor(ActorRef(kind="profile", id=getattr(task, name)))
            for dep_id in task.deps:
                dep = self.get("tasks", dep_id)
                require(dep.root_id == task.root_id and dep.project_id == task.project_id, "dependency crosses root/project")
                if task.status in {"ready", "running", "verifying", "waiting_user", "waiting_agent", "completed", "unverified"}:
                    require(dep.status == "completed", "execution requires completed dependencies")
            budget = self.get("budget_scopes", task.budget_scope_id)
            self.same_task(budget, task.task_id)
            if task.status == "ready":
                current_scope = budget
                while True:
                    require(current_scope.expires_at is None or time_value(task.updated_at) < time_value(current_scope.expires_at), "ready/retry task exceeds budget deadline")
                    require(current_scope.active_used_ms < current_scope.active_limit_ms, "ready/retry task exhausted execution time")
                    family = self.descendants(current_scope.task_id)
                    consumption = {"tokens": 0, "llm_calls": 0, "tool_calls": 0}
                    for lease in self.s.budget_leases:
                        if lease.task_id in family and lease.status != "released":
                            quantity = lease.actual if lease.status == "settled" else lease.reserved
                            for dimension in consumption:
                                consumption[dimension] += getattr(quantity, dimension)
                    remaining = {k: getattr(current_scope.limits, k) - v for k, v in consumption.items()}
                    require(remaining["tool_calls"] > 0 or (remaining["llm_calls"] > 0 and remaining["tokens"] > 0), "ready/retry task has no remaining call allowance")
                    if current_scope.parent_scope_id is None:
                        break
                    current_scope = self.get("budget_scopes", current_scope.parent_scope_id)
            spec = self.spec(task.acceptance_ref)
            if spec is not None:
                self.same_task(spec, task.task_id)
            for ref in task.input_artifact_refs + task.output_artifact_refs:
                self.artifact(ref, task)
            if task.current_attempt_id is not None:
                self.same_task(self.get("attempts", task.current_attempt_id), task.task_id)
            if task.latest_report_id is not None:
                self.same_task(self.get("acceptance_reports", task.latest_report_id), task.task_id, task.current_attempt_id)
            if task.wait_ref is not None:
                self.validate_wait(task, task.wait_ref, task.current_attempt_id)
            if task.status in TASK_CLOSED:
                family = self.descendants(task.task_id)
                require(all(t.status in TASK_CLOSED for t in self.s.tasks if t.task_id in family), "closed task has open descendants")
                require(not any(a.status in {"running", "unknown"} for a in self.s.actions if a.task_id in family), "closed task has unresolved external actions")
                closed = time_value(task.updated_at)
                require(all(time_value(t.updated_at) <= closed for t in self.s.tasks if t.task_id in family), "task closes before a descendant")
                if task.current_attempt_id is not None:
                    attempt = self.get("attempts", task.current_attempt_id)
                    require(attempt.ended_at is not None and time_value(attempt.ended_at) <= closed, "task closes before its attempt")
                if task.latest_report_id is not None:
                    require(time_value(self.get("acceptance_reports", task.latest_report_id).created_at) <= closed, "task closes before its report")
            if task.status == "completed":
                report = self.get("acceptance_reports", task.latest_report_id)
                require(report.verdict == "passed" and report.spec_ref == task.acceptance_ref, "completed task requires its current passed report")
                require(spec.constraints_ref == task.constraints_ref, "completion uses stale constraints")
                checked = {x.artifact_id: x.content_hash for x in report.evaluated_artifact_refs}
                require(all(checked.get(x.artifact_id) == x.content_hash for x in task.output_artifact_refs), "report does not cover current outputs")

    def validate_wait(self, task: TaskRecord, wait: Any, attempt_id: str | None, *, historic: bool = False) -> None:
        if wait.kind == "human":
            request = self.get("human_requests", wait.request_id)
            self.same_task(request, task.task_id, attempt_id)
            if not historic:
                require(request.status == "pending", "waiting_user requires a pending request")
        elif wait.kind == "children":
            for identity in wait.child_task_ids:
                child = self.get("tasks", identity)
                require(child.parent_id == task.task_id, "waiting child belongs to another parent")
        else:
            action = self.get("actions", wait.action_id)
            self.same_task(action, task.task_id, attempt_id)
            if not historic:
                require(action.status == "unknown", "external wait requires an unknown action")

    def validate_execution(self) -> None:
        for task in self.s.tasks:
            attempts = [a for a in self.s.attempts if a.task_id == task.task_id]
            require(sum(a.state not in ATTEMPT_CLOSED for a in attempts) <= 1, "multiple open attempts for one task")
            if attempts:
                require(task.current_attempt_id is not None, "task does not identify its latest attempt")
                current = self.get("attempts", task.current_attempt_id)
                require(current.number == max(a.number for a in attempts), "current attempt is not the latest")
                expected = {"completed": {"succeeded"}, "failed": {"failed"}, "cancelled": {"cancelled"}, "unverified": {"unverified"}, "running": {"running"}, "verifying": {"running"}, "waiting_user": {"suspended"}, "waiting_agent": {"suspended"}, "ready": {"pending", "suspended"}}
                if task.status in expected:
                    require(current.state in expected[task.status], "task/attempt lifecycle mismatch")
        for attempt in self.s.attempts:
            self.get("tasks", attempt.task_id)
            if attempt.context_ref is not None:
                self.catalog(attempt.context_ref)
            if attempt.checkpoint_id is not None:
                self.same_task(self.get("checkpoints", attempt.checkpoint_id), attempt.task_id, attempt.attempt_id)
            for rid in attempt.report_ids:
                self.same_task(self.get("acceptance_reports", rid), attempt.task_id, attempt.attempt_id)
            runs = [r for r in self.s.runs if r.attempt_id == attempt.attempt_id]
            require(sum(r.state == "running" for r in runs) <= 1, "multiple active runs for one attempt")
            require((sum(r.state == "running" for r in runs) == 1) == (attempt.state == "running"), "attempt/run activity mismatch")
            if attempt.state in ATTEMPT_CLOSED:
                require(all(r.state in {"ended", "crashed"} for r in runs), "closed attempt has open runs")
            started = sorted((r for r in runs if r.started_at is not None), key=lambda r: time_value(r.started_at))
            for previous, following in zip(started, started[1:]):
                require(previous.ended_at is not None and time_value(previous.ended_at) <= time_value(following.started_at), "overlapping runs within one attempt")
        for run in self.s.runs:
            attempt = self.get("attempts", run.attempt_id)
            self.same_task(attempt, run.task_id)
            if run.started_at is not None:
                require(attempt.started_at is not None and time_value(run.started_at) >= time_value(attempt.started_at), "run precedes attempt")
            if run.ended_at is not None and attempt.ended_at is not None:
                require(time_value(run.ended_at) <= time_value(attempt.ended_at), "run outlives attempt")
            if run.resumed_from is not None:
                cp = self.get("checkpoints", run.resumed_from)
                self.same_task(cp, run.task_id, run.attempt_id)
                require(cp.run_id != run.run_id and time_value(cp.created_at) <= time_value(run.created_at), "invalid resume checkpoint")

    def validate_acceptance(self) -> None:
        for spec in self.s.acceptance_specs:
            task = self.get("tasks", spec.task_id)
            require(spec.target.project_id == task.project_id, "acceptance target crosses project")
            self.catalog(spec.target.environment_id, {"environment", "controlled_fixture", "private_staging_fixture"})
            for identity in spec.target.resource_ids:
                require(self.resource_environment(identity, task.project_id) == spec.target.environment_id, "acceptance resource belongs to another environment")
            self.version(spec.constraints_ref, {"constraints"})
            evaluator = self.version(spec.evaluator_ref, {"evaluator"})
            require(evaluator.get("evidence_kind") == spec.evaluator_ref.kind, "evaluator evidence kind mismatch")
            self.authority(spec.authority_ref, task.project_id, spec=True)
        for report in self.s.acceptance_reports:
            task = self.get("tasks", report.task_id)
            self.same_task(self.get("attempts", report.attempt_id), report.task_id)
            spec = self.spec(report.spec_ref)
            validate_report(spec, report)
            if spec is not None and report.verdict == "passed":
                require(set(spec.target.resource_ids).issubset({x.resource_id for x in report.evaluated_resource_versions}), "passed report does not cover target resources")
                roles = {self.artifact(x, task).role for x in report.evaluated_artifact_refs}
                require(set(spec.required_output_roles).issubset(roles), "passed report lacks required output roles")
            for value in report.evaluated_resource_versions:
                resource = self.catalog(value.resource_id, {"resource"})
                require(resource.get("project_id") == task.project_id, "evaluated resource crosses project")
            for value in report.evaluated_artifact_refs:
                artifact = self.artifact(value, task)
                require(time_value(artifact.created_at) <= time_value(report.evaluated_at), "evaluation references a future artifact")
            for value in report.evidence_refs:
                self.evidence(value, task)
            for check in report.checks:
                for value in check.evidence_refs:
                    self.evidence(value, task)

    def validate_artifacts(self) -> None:
        for artifact in self.s.artifacts:
            root = self.get("tasks", artifact.scope.root_task_id)
            require(root.parent_id is None, "artifact scope is not a root")
            self.same_scope(root, artifact.scope)
            creator = artifact.creator_ref
            if isinstance(creator, TaskCreatorRef):
                task = self.get("tasks", creator.task_id)
                self.same_scope(task, artifact.scope)
                self.same_task(self.get("attempts", creator.attempt_id), task.task_id)
                if creator.action_id is not None:
                    self.same_task(self.get("actions", creator.action_id), task.task_id, creator.attempt_id)
            else:
                self.actor(creator.actor)
                self.catalog(creator.source_id)
            for reference in artifact.input_refs:
                parent = self.artifact(reference, root)
                require(time_value(parent.created_at) <= time_value(artifact.created_at), "artifact predates its inputs")
            for reference in artifact.source_refs:
                self.evidence(reference, root)
        for identity, content in self.s.artifact_contents_utf8.items():
            require(type(content) is str, "artifact_contents_utf8 requires strings")
            validate_artifact_content(self.get("artifacts", identity), content.encode("utf-8"))
        for grant in self.s.artifact_grants:
            self.actor(grant.subject)
            self.authority(grant.authority_ref, grant.project_id)
            for tid in grant.task_ids:
                task = self.get("tasks", tid)
                require(task.project_id == grant.project_id, "grant task crosses project")
                for aid in grant.artifact_ids:
                    artifact = self.get("artifacts", aid)
                    self.same_scope(task, artifact.scope)
                    require(time_value(artifact.created_at) <= time_value(grant.created_at), "grant predates artifact")

    def validate_human_actions(self) -> None:
        for request in self.s.human_requests:
            self.same_task(self.get("attempts", request.attempt_id), request.task_id)
            self.actor(request.requested_from)
            if request.action_id is not None:
                action = self.get("actions", request.action_id)
                self.same_task(action, request.task_id, request.attempt_id)
                require(request.action_digest == action.intent_digest, "request action digest mismatch")
            if request.reply_id is not None:
                validate_reply(request, self.get("human_replies", request.reply_id))
        for reply in self.s.human_replies:
            request = self.get("human_requests", reply.request_id)
            require(request.reply_id == reply.reply_id and request.status == "answered", "orphan or conflicting accepted reply")
            validate_reply(request, reply)
        for action in self.s.actions:
            task = self.get("tasks", action.task_id)
            self.same_task(self.get("attempts", action.attempt_id), task.task_id)
            self.same_scope(task, action.scope)
            self.version(action.constraints_ref, {"constraints"})
            for value in action.expected_resources:
                resource = self.catalog(value.resource_id, {"resource"})
                require(resource.get("project_id") == task.project_id, "action resource crosses project")
            if action.approval_request_id is not None:
                request = self.get("human_requests", action.approval_request_id)
                self.same_task(request, action.task_id, action.attempt_id)
                require(request.action_id == action.action_id and request.action_digest == action.intent_digest, "action request binding mismatch")
            if action.lease_id is not None:
                lease = self.get("budget_leases", action.lease_id)
                require(lease.kind == "tool" and lease.run_id == action.execution_run_id, "action must use its own tool lease/run")
                self.same_task(lease, action.task_id, action.attempt_id)
                require(lease.dispatched_at is not None, "executed action requires dispatched lease")
                auth = self.catalog(action.authorization_ref, {"authorization_decision"})
                policy = self.version(VersionRef.model_validate(auth.get("policy_ref")), {"policy"})
                require(policy.get("project_id", task.project_id) == task.project_id, "authorization policy crosses project")
                require(auth.get("action_id") == action.action_id and auth.get("task_id") == task.task_id and auth.get("attempt_id") == action.attempt_id and auth.get("intent_digest") == action.intent_digest and auth.get("decision") == "allow", "authorization does not bind the executed action")
                require(auth.get("scope") == action.scope.model_dump(mode="json"), "authorization scope mismatch")
                subject = ActorRef.model_validate(auth.get("subject"))
                self.actor(subject)
                require(subject.kind == "profile", "execution authorization requires a profile subject")
                # The current assignee can legitimately change after a historic
                # action. The immutable authorization binds its original actor.
                checked_at = _time(auth.get("checked_at"))
                require(checked_at >= time_value(action.created_at), "authorization precedes action intent")
                require(checked_at <= time_value(lease.dispatched_at), "authorization follows execution")
                if action.approval_request_id is not None:
                    reply = self.get("human_replies", request.reply_id)
                    validate_approval(action, request, reply)
                    require(time_value(reply.replied_at) <= time_value(lease.dispatched_at), "action was dispatched before approval")
                    require(max(time_value(reply.created_at), time_value(request.updated_at)) <= checked_at, "authorization precedes the persisted approval")
                    require(auth.get("reply_id") == reply.reply_id, "authorization references another reply")
                    if request.expires_at is not None:
                        require(time_value(lease.dispatched_at) < time_value(request.expires_at), "approval expired before execution")
            if action.receipt_ref is not None:
                self.evidence(action.receipt_ref, task)
                receipt = self.catalog(action.receipt_ref.id)
                require(receipt.get("action_id") == action.action_id, "receipt belongs to another action")
                require(receipt.get("task_id", action.task_id) == action.task_id, "receipt belongs to another task")
                require(lease.dispatched_at is not None and time_value(lease.dispatched_at) <= _time(receipt.get("at")) <= time_value(action.updated_at), "receipt is outside the execution result interval")
                if action.status == "succeeded":
                    require(receipt.get("ok") is True, "succeeded action requires a successful receipt")

    def validate_budgets(self) -> None:
        for scope in self.s.budget_scopes:
            task = self.get("tasks", scope.task_id)
            require(task.budget_scope_id == scope.scope_id, "task/budget ownership mismatch")
            root = self.get("budget_scopes", scope.root_scope_id)
            require(root.task_id == task.root_id and root.parent_scope_id is None, "invalid budget root")
            if scope.parent_scope_id is not None:
                parent = self.get("budget_scopes", scope.parent_scope_id)
                require(parent.task_id == task.parent_id and parent.root_scope_id == scope.root_scope_id, "budget parent differs from task parent")
                if parent.expires_at is not None:
                    require(scope.expires_at is not None and time_value(scope.expires_at) <= time_value(parent.expires_at), "child extends parent deadline")
            family = self.descendants(task.task_id)
            runs = [r for r in self.s.runs if r.task_id in family and r.started_at is not None]
            if all(r.ended_at is not None for r in runs):
                intervals = sorted((time_value(r.started_at), time_value(r.ended_at)) for r in runs)
                merged: list[tuple[Any, Any]] = []
                for start, end in intervals:
                    if merged and start <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                    else:
                        merged.append((start, end))
                actual = round(sum((b - a).total_seconds() * 1000 for a, b in merged))
                require(scope.active_used_ms == actual, "closed-run active time must equal interval union")
        for lease in self.s.budget_leases:
            scope = self.get("budget_scopes", lease.scope_id)
            run = self.get("runs", lease.run_id)
            self.same_task(scope, lease.task_id)
            self.same_task(run, lease.task_id, lease.attempt_id)
            if lease.dispatched_at is not None:
                dispatched = time_value(lease.dispatched_at)
                require(run.started_at is not None and dispatched >= time_value(run.started_at), "dispatch precedes run")
                if run.ended_at is not None:
                    require(dispatched <= time_value(run.ended_at), "dispatch follows run")
                if scope.expires_at is not None:
                    require(dispatched < time_value(scope.expires_at), "request dispatched after deadline")
            if lease.evidence_ref is not None:
                self.evidence(lease.evidence_ref, self.get("tasks", lease.task_id))

    def validate_checkpoints(self) -> None:
        for cp in self.s.checkpoints:
            task = self.get("tasks", cp.task_id)
            self.same_task(self.get("attempts", cp.attempt_id), cp.task_id)
            run = self.get("runs", cp.run_id)
            self.same_task(run, cp.task_id, cp.attempt_id)
            self.catalog(cp.context_ref)
            self.catalog(cp.history_ref)
            self.version(cp.constraints_ref, {"constraints"})
            self.same_task(self.get("budget_scopes", cp.budget_scope_id), cp.task_id)
            checkpoint_time = time_value(cp.created_at)
            require(run.started_at is not None and time_value(run.started_at) <= checkpoint_time, "checkpoint precedes its run")
            if run.ended_at is not None:
                require(checkpoint_time <= time_value(run.ended_at), "checkpoint follows its run")
            for identity in cp.applied_message_ids:
                message = self.catalog(identity)
                if "at" in message:
                    require(_time(message["at"]) <= checkpoint_time, "checkpoint applies a future message")
            if cp.wait_ref is not None:
                self.validate_wait(task, cp.wait_ref, cp.attempt_id, historic=True)
                if cp.wait_ref.kind == "human":
                    require(time_value(self.get("human_requests", cp.wait_ref.request_id).created_at) <= checkpoint_time, "checkpoint precedes its request")
            for identity in cp.completed_action_ids + cp.resume_cursor.pending_action_ids:
                action = self.get("actions", identity)
                self.same_task(action, cp.task_id, cp.attempt_id)
                require(time_value(action.created_at) <= checkpoint_time, "checkpoint references a future action")
            for identity in cp.completed_action_ids:
                action = self.get("actions", identity)
                require(action.status in {"succeeded", "failed", "cancelled"} and time_value(action.updated_at) <= checkpoint_time, "checkpoint claims a future/unresolved action completed")
            for call_id in cp.resume_cursor.observed_tool_call_ids:
                for action in self.s.actions:
                    if (action.task_id, action.attempt_id, action.tool_call_id) == (cp.task_id, cp.attempt_id, call_id):
                        require(action.status in {"succeeded", "failed", "cancelled"} and time_value(action.updated_at) <= checkpoint_time, "checkpoint observes a future/unresolved action")
                for request in self.s.human_requests:
                    if (request.task_id, request.attempt_id, request.continuation.tool_call_id) == (cp.task_id, cp.attempt_id, call_id):
                        require(request.reply_id is not None and time_value(self.get("human_replies", request.reply_id).created_at) <= checkpoint_time, "checkpoint observes a future reply")
            for pending in cp.pending_delegations:
                require(self.get("tasks", pending.child_task_id).parent_id == cp.task_id, "checkpoint delegates another parent's child")
        for receipt in self.s.delivery_receipts:
            child = self.get("tasks", receipt.child_task_id)
            require(child.parent_id == receipt.parent_task_id, "delivery parent mismatch")
            self.same_task(self.get("attempts", receipt.attempt_id), receipt.parent_task_id)
            report = self.get("acceptance_reports", receipt.report_id)
            self.same_task(report, child.task_id)
            require(time_value(report.created_at) <= time_value(receipt.delivered_at), "result delivered before report")
            require(any(p.delivery_key == receipt.delivery_key and p.child_task_id == child.task_id for cp in self.s.checkpoints if cp.attempt_id == receipt.attempt_id for p in cp.pending_delegations), "delivery has no matching saved delegation")
