"""Integration and lifecycle regressions for the isolated M0-01 contract layer."""
from __future__ import annotations

import copy
import unittest

from pydantic import ValidationError

from miniagent.contracts import (
    AcceptanceSpec, ActionRecord, ActorRef, ArtifactRef, BudgetLease,
    ContractSnapshot, HumanRequest, TaskRecord, TransitionError,
    action_intent_digest, canonical_hash, parse_record, transition_record,
    validate_transition,
)
from tests.contracts_support import example_data, example_record


def new_task(**changes):
    data = example_record("tasks", "T0")
    data.update(status="created", revision=1, updated_at=data["created_at"],
                current_attempt_id=None, latest_report_id=None, output_artifact_refs=[],
                wait_ref=None, reason=None)
    data.update(changes)
    return TaskRecord.model_validate(data)


def prepared_action():
    data = example_record("actions", "X4")
    data.update(status="prepared", revision=1, updated_at=data["created_at"],
                authorization_ref=None, execution_run_id=None, lease_id=None,
                receipt_ref=None, reason=None)
    return ActionRecord.model_validate(data)


class ContractWireTest(unittest.TestCase):
    def test_every_documented_record_uses_public_parser(self):
        data = example_data()
        count = 0
        for values in data.values():
            if not isinstance(values, list):
                continue
            for value in values:
                if "record_type" in value:
                    with self.subTest(record=value["record_type"]):
                        record = parse_record(value)
                        self.assertEqual(record.model_dump(mode="json"), value)
                        self.assertEqual(type(record).model_validate_json(record.model_dump_json()), record)
                        count += 1
        self.assertEqual(count, 82)

    def test_wire_rejects_unknown_fields_versions_and_type_coercion(self):
        for field, value in [("schema_version", True), ("schema_version", 2),
                             ("revision", "2"), ("revision", 2.0), ("task_id", 8),
                             ("objective", "  "), ("record_type", "NotARecord"),
                             ("owner", "surprise")]:
            with self.subTest(field=field, value=value):
                data = new_task().model_dump(mode="json")
                data[field] = value
                with self.assertRaises(ValidationError):
                    parse_record(data)

    def test_utc_timestamp_validation(self):
        for value in ["2026-02-30T10:00:00Z", "2026-11-15", "2026-11-15T10:00:00", "2026-11-15T10:00:00+08:00", 1234]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                new_task(created_at=value)
        self.assertEqual(new_task(created_at="2026-11-15T10:00:00+00:00").created_at, "2026-11-15T10:00:00Z")

    def test_snapshots_do_not_leak_mutable_containers(self):
        action = prepared_action()
        with self.assertRaises(TypeError):
            action.args["resource_id"] = "other"
        with self.assertRaises(ValidationError):
            action.operation = "delete"
        spec_data = example_record("acceptance_specs")
        spec_data["params"] = {"nested": [{"x": 1}]}
        spec = AcceptanceSpec.model_validate(spec_data)
        with self.assertRaises(TypeError):
            spec.params["nested"][0]["x"] = 2
        spec_data["params"]["nested"][0]["x"] = 3
        self.assertEqual(spec.params["nested"][0]["x"], 1)
        self.assertEqual(spec.model_dump(mode="json")["params"], {"nested": [{"x": 1}]})

    def test_nullable_fields_remain_required_in_schema(self):
        schema = TaskRecord.model_json_schema()
        self.assertIn("wait_ref", schema["required"])
        data = new_task().model_dump(mode="json")
        del data["wait_ref"]
        with self.assertRaises(ValidationError):
            TaskRecord.model_validate(data)


class TransitionTest(unittest.TestCase):
    def test_task_claim_returns_new_snapshot(self):
        before = new_task()
        ready = transition_record(before, "ready", expected_revision=1, at="2026-11-15T10:00:01Z")
        running = transition_record(ready, "running", expected_revision=2, at="2026-11-15T10:00:02Z", current_attempt_id="AT-new")
        self.assertEqual((before.status, before.revision), ("created", 1))
        self.assertEqual((running.status, running.revision), ("running", 3))

    def test_rejects_state_skips_stale_revision_and_clock_reversal(self):
        before = new_task()
        with self.assertRaisesRegex(TransitionError, "not allowed"):
            transition_record(before, "completed", expected_revision=1, at=before.updated_at,
                              current_attempt_id="AT0", latest_report_id="R0")
        for revision in [0, 2, True]:
            with self.subTest(revision=revision), self.assertRaisesRegex(TransitionError, "revision_conflict"):
                transition_record(before, "ready", expected_revision=revision, at=before.updated_at)
        ready = transition_record(before, "ready", expected_revision=1, at="2026-11-15T10:00:05Z")
        with self.assertRaisesRegex(TransitionError, "backwards"):
            transition_record(ready, expected_revision=2, at="2026-11-15T10:00:04Z")

    def test_resume_preserves_attempt_but_retry_replaces_it(self):
        before = new_task(status="waiting_user", current_attempt_id="AT0",
                          wait_ref={"kind": "human", "request_id": "H-question"})
        with self.assertRaisesRegex(TransitionError, "keep the current attempt"):
            transition_record(before, "ready", expected_revision=1, at=before.updated_at,
                              wait_ref=None, current_attempt_id="AT-new")
        resumed = transition_record(before, "ready", expected_revision=1, at=before.updated_at, wait_ref=None)
        self.assertEqual(resumed.current_attempt_id, "AT0")
        failed = new_task(status="failed", reason="failed_acceptance", current_attempt_id="AT0")
        with self.assertRaises(TransitionError):
            transition_record(failed, "ready", expected_revision=1, at=failed.updated_at,
                              reason=None, current_attempt_id="AT-new")
        retried = transition_record(failed, "ready", expected_revision=1, at=failed.updated_at,
                                    command="retry", reason=None, current_attempt_id="AT-new")
        self.assertEqual(retried.budget_scope_id, failed.budget_scope_id)
        with self.assertRaisesRegex(TransitionError, "immutable"):
            transition_record(failed, "ready", expected_revision=1, at=failed.updated_at,
                              command="retry", reason=None, current_attempt_id="AT-new", budget_scope_id="fresh-budget")

    def test_completed_task_and_immutable_records_cannot_be_reopened(self):
        completed = TaskRecord.model_validate(example_record("tasks", "T0"))
        with self.assertRaises(TransitionError):
            transition_record(completed, "running", expected_revision=completed.revision, at=completed.updated_at)
        spec = AcceptanceSpec.model_validate(example_record("acceptance_specs"))
        with self.assertRaisesRegex(TransitionError, "immutable"):
            validate_transition(spec, spec, expected_revision=1)

    def test_changing_approved_intent_is_rejected_even_with_recomputed_hashes(self):
        before = prepared_action()
        args = before.model_dump(mode="json")["args"]
        args["resource_id"] = "other-config"
        ah = canonical_hash(args)
        ih = action_intent_digest(operation=before.operation, effect=before.effect,
                                  args_hash=ah, expected_resources=before.expected_resources,
                                  scope=before.scope, constraints_ref=before.constraints_ref)
        with self.assertRaisesRegex(TransitionError, "immutable"):
            transition_record(before, expected_revision=1, at=before.updated_at,
                              args=args, args_hash=ah, intent_digest=ih)

    def test_unknown_lease_does_not_reset_to_reserved(self):
        data = example_record("budget_leases")
        data.update(status="unknown", actual=None, settled_at=None, evidence_ref=None, reason="usage_unknown")
        unknown = BudgetLease.model_validate(data)
        with self.assertRaises(TransitionError):
            transition_record(unknown, "reserved", expected_revision=unknown.revision,
                              at=unknown.updated_at, dispatched_at=None, reason=None)
        released = transition_record(unknown, "released", expected_revision=unknown.revision,
                                     at=unknown.updated_at, dispatched_at=None,
                                     actual={"tokens": 0, "llm_calls": 0, "tool_calls": 0},
                                     settled_at=unknown.updated_at,
                                     evidence_ref={"kind": "catalog", "id": "confirmed-not-sent"},
                                     reason="reconciled_not_sent")
        self.assertEqual(released.status, "released")

    def test_pending_request_cannot_be_expired_early(self):
        data = example_record("human_requests")
        data.update(status="pending", reply_id=None, revision=1, updated_at=data["created_at"])
        request = HumanRequest.model_validate(data)
        with self.assertRaises(ValueError):
            transition_record(request, "expired", expected_revision=1, at=request.created_at)
        expired = transition_record(request, "expired", expected_revision=1, at=request.expires_at)
        with self.assertRaises(TransitionError):
            transition_record(expired, "answered", expected_revision=2, at=request.expires_at,
                              reply_id="RP-question")

    def test_unchecked_pydantic_copy_does_not_bypass_transition_validation(self):
        before = new_task()
        forged = before.model_copy(update={"status": "ready", "revision": True})
        with self.assertRaises(ValidationError):
            validate_transition(before, forged, expected_revision=1)


class SnapshotTest(unittest.TestCase):
    def test_documented_snapshot_round_trip_and_totals(self):
        data = example_data()
        snapshot = ContractSnapshot.model_validate(data)
        self.assertEqual(snapshot.model_dump(mode="json"), data)
        self.assertEqual(sum(l.actual.tokens for l in snapshot.budget_leases), 7600)
        self.assertEqual(snapshot.budget_scopes[0].active_used_ms, 100000)

    def assert_bad(self, edit, message=None):
        data = example_data()
        edit(data)
        if message is None:
            with self.assertRaises(ValueError):
                ContractSnapshot.model_validate(data)
        else:
            with self.assertRaisesRegex(ValueError, message):
                ContractSnapshot.model_validate(data)

    def test_rejects_duplicate_task_and_call_id(self):
        self.assert_bad(lambda d: d["tasks"].append(copy.deepcopy(d["tasks"][0])), "duplicate")
        self.assert_bad(lambda d: d["budget_leases"][1].update(call_id=d["budget_leases"][0]["call_id"]), "duplicate")

    def test_rejects_dependency_and_parent_cycles(self):
        self.assert_bad(lambda d: d["tasks"][1].update(deps=["T2"]), "cycle")
        self.assert_bad(lambda d: d["tasks"][1].update(parent_id="T2") or d["tasks"][2].update(parent_id="T1"), "cycle")

    def test_rejects_dangling_artifact_and_changed_bytes(self):
        self.assert_bad(lambda d: d["tasks"][0]["input_artifact_refs"][0].update(artifact_id="missing"), "invalid_reference")
        self.assert_bad(lambda d: d["artifact_contents_utf8"].update(F0="corrupted"), "does not match")

    def test_completion_rejects_stale_constraints_or_unchecked_outputs(self):
        def stale(d):
            d["catalog"]["CON-1"]["versions"] = [1, 2]
            d["tasks"][0]["constraints_ref"]["version"] = 2
        self.assert_bad(stale, "stale constraints")
        self.assert_bad(lambda d: d["tasks"][0]["output_artifact_refs"].append(d["tasks"][0]["input_artifact_refs"][0]), "cover current outputs")

    def test_child_cannot_extend_budget_deadline(self):
        self.assert_bad(lambda d: d["budget_scopes"][1].update(expires_at="2026-11-15T11:00:00Z"), "extends parent")

    def test_denied_reply_cannot_authorize_executed_action(self):
        self.assert_bad(lambda d: d["human_replies"][1].update(decision="deny"), "decision allow")

    def test_action_cannot_use_another_attempts_lease(self):
        self.assert_bad(lambda d: d["actions"][3].update(lease_id="LT1"), "lease/run")

    def test_duplicate_open_attempt_and_running_child_under_closed_root(self):
        def open_child(d):
            d["tasks"][1].update(status="running")
        self.assert_bad(open_child, "open descendants")

    def test_staging_report_cannot_claim_other_environment_resources(self):
        def wrong_target(d):
            r = next(r for r in d["acceptance_reports"] if r["report_id"] == "R2")
            r["evaluated_resource_versions"][0]["resource_id"] = "RESOURCE-config"
        self.assert_bad(wrong_target, "cover target resources")

    def test_spec_environment_cannot_relabel_a_private_snapshot(self):
        self.assert_bad(lambda d: d["acceptance_specs"][2]["target"].update(environment_id="env-fixture-A"), "another environment")

    def test_typed_versioned_catalog_and_authority_sources(self):
        for edit, message in [
            (lambda d: d["catalog"]["CON-1"].update(version=True), "positive integers"),
            (lambda d: d["catalog"]["E-S0"].update(kind="user"), "wrong catalog type"),
            (lambda d: d["catalog"]["E-S0"].update(evidence_kind="llm"), "evidence kind"),
            (lambda d: d["artifact_grants"][0].update(authority_ref="RESOURCE-config"), "wrong catalog type"),
            (lambda d: d["catalog"]["AUTH-X4"].update(policy_ref={"id": "missing", "version": 1}), "invalid_reference"),
        ]:
            with self.subTest(message=message):
                self.assert_bad(edit, message)

    def test_authorization_cannot_precede_persisted_approval(self):
        self.assert_bad(lambda d: d["catalog"]["AUTH-X4"].update(checked_at="2026-11-15T10:02:00Z"), "persisted approval")

        def late_persistence(d):
            d["human_replies"][1]["created_at"] = "2026-11-15T10:03:36Z"
            d["human_requests"][1]["updated_at"] = "2026-11-15T10:03:36Z"
        self.assert_bad(late_persistence, "persisted approval")

    def test_action_receipt_must_belong_to_that_action(self):
        self.assert_bad(lambda d: d["actions"][3].update(receipt_ref={"kind": "receipt", "id": "RC-X1"}), "another action")

    def test_completed_runs_in_one_attempt_cannot_overlap(self):
        def overlapping(d):
            run = copy.deepcopy(d["runs"][0])
            run["run_id"] = "RUN-duplicate"
            d["runs"].append(run)
        self.assert_bad(overlapping, "overlapping runs")

    def test_checkpoint_cannot_precede_its_run_or_complete_a_future_action(self):
        self.assert_bad(lambda d: d["checkpoints"][2].update(created_at="2026-11-15T09:00:00Z"), "precedes its run")

        def future_action(d):
            cp = d["checkpoints"][2]
            cp["completed_action_ids"] = ["X4"]
            cp["resume_cursor"].update(phase="before_model", pending_action_ids=[],
                                       pending_tool_call_ids=[], observed_tool_call_ids=["TC-X4"])
        self.assert_bad(future_action, "future/unresolved")

    def test_root_cannot_close_before_children_and_evidence(self):
        self.assert_bad(lambda d: d["tasks"][0].update(updated_at="2026-11-15T10:00:00Z"), "closes before")

    def test_child_cannot_depend_on_ancestor_completion(self):
        self.assert_bad(lambda d: d["tasks"][2].update(deps=["T0"]), "cycle")

    def test_explicit_retry_still_requires_root_deadline_and_available_budget(self):
        def retry(d, at):
            task = d["tasks"][0]
            old = next(a for a in d["attempts"] if a["attempt_id"] == "AT0")
            old.update(state="failed", end_reason="retry_requested")
            new = copy.deepcopy(old)
            new.update(attempt_id="AT0-retry", number=2, state="pending", revision=1,
                       created_at=at, updated_at=at, context_ref=None, checkpoint_id=None,
                       report_ids=[], started_at=None, ended_at=None, end_reason=None)
            d["attempts"].append(new)
            task.update(status="ready", reason=None, current_attempt_id="AT0-retry",
                        latest_report_id=None, output_artifact_refs=[], revision=task["revision"] + 1,
                        updated_at=at)
        self.assert_bad(lambda d: retry(d, "2026-11-15T10:11:00Z"), "budget deadline")

        def exhausted(d):
            retry(d, "2026-11-15T10:08:00Z")
            d["budget_scopes"][0]["limits"] = {"tokens": 7600, "llm_calls": 7, "tool_calls": 6}
        self.assert_bad(exhausted, "remaining call allowance")

    def test_artifact_access_uses_exact_current_grant(self):
        data = example_data()
        snapshot = ContractSnapshot.model_validate(data)
        artifact = next(a for a in snapshot.artifacts if a.artifact_id == "F5")
        arguments = dict(subject=ActorRef(kind="profile", id="profile-wei"), task_id="T0",
                         artifact_ref=ArtifactRef(artifact_id="F5", content_hash=artifact.content_hash),
                         operation="read", at="2026-11-15T10:05:00Z")
        snapshot.validate_artifact_access(**arguments)
        for grant in data["artifact_grants"]:
            if grant["grant_id"] == "G-T0-F5":
                grant.update(status="revoked", revision=2, updated_at="2026-11-15T10:04:30Z")
        revoked = ContractSnapshot.model_validate(data)
        with self.assertRaisesRegex(ValueError, "permission_denied"):
            revoked.validate_artifact_access(**arguments)
        with self.assertRaises(ValueError):
            snapshot.validate_artifact_access(**{**arguments, "subject": ActorRef(kind="profile", id="profile-env")})


if __name__ == "__main__":
    unittest.main()
