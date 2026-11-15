"""Offline examples for opt-in task and budget snapshot validation."""
from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from miniagent.contracts.budget import BudgetLease, BudgetScope, Quantity
from miniagent.contracts.tasks import (
    AttemptRecord, Checkpoint, PendingDelegation, ResumeCursor, RunRecord,
    TaskRecord, WaitRef,
)


def example_data() -> dict:
    path = Path(__file__).resolve().parents[1] / "docs" / "m0-01-computer-maintenance-example.md"
    data = {}
    for block in re.findall(r"```json\s*\n(.*?)\n```", path.read_text(encoding="utf-8"), re.DOTALL):
        data.update(json.loads(block))
    return data


class TaskBudgetContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = example_data()

    def payload(self, collection, index=0, **updates):
        value = copy.deepcopy(self.data[collection][index])
        value.update(updates)
        return value

    def assert_invalid(self, model, payload):
        with self.assertRaises(ValidationError):
            model.model_validate(payload)

    def test_documented_snapshots_round_trip_without_losing_nulls_or_arrays(self):
        for collection, model in (
            ("tasks", TaskRecord), ("attempts", AttemptRecord), ("runs", RunRecord),
            ("budget_scopes", BudgetScope), ("budget_leases", BudgetLease),
            ("checkpoints", Checkpoint),
        ):
            for payload in self.data[collection]:
                with self.subTest(collection=collection, id=next(iter(payload.values()))):
                    record = model.model_validate(payload)
                    self.assertEqual(record.model_dump(mode="json"), payload)
                    self.assertEqual(model.model_validate_json(record.model_dump_json()), record)

    def test_required_nullable_fields_cannot_be_omitted(self):
        for collection, model, field in (
            ("tasks", TaskRecord, "wait_ref"),
            ("attempts", AttemptRecord, "checkpoint_id"),
            ("runs", RunRecord, "resumed_from"),
            ("budget_scopes", BudgetScope, "parent_scope_id"),
            ("budget_leases", BudgetLease, "reason"),
            ("checkpoints", Checkpoint, "wait_ref"),
        ):
            with self.subTest(model=model.__name__, field=field):
                value = self.payload(collection)
                del value[field]
                self.assert_invalid(model, value)

    def test_unknown_fields_and_states_are_rejected(self):
        self.assert_invalid(TaskRecord, self.payload("tasks", arbitrary_permission=True))
        self.assert_invalid(TaskRecord, self.payload("tasks", status="done"))
        self.assert_invalid(BudgetLease, self.payload("budget_leases", status="refunded"))

    def test_completed_task_requires_current_attempt_spec_and_report(self):
        for field in ("current_attempt_id", "acceptance_ref", "latest_report_id"):
            with self.subTest(field=field):
                self.assert_invalid(TaskRecord, self.payload("tasks", **{field: None}))

    def test_root_parent_and_self_dependency_are_not_interchangeable(self):
        self.assert_invalid(TaskRecord, self.payload("tasks", parent_id="T9"))
        self.assert_invalid(TaskRecord, self.payload("tasks", 1, parent_id=None))
        self.assert_invalid(TaskRecord, self.payload("tasks", 1, parent_id="T1"))
        self.assert_invalid(TaskRecord, self.payload("tasks", deps=["T0"]))
        self.assert_invalid(TaskRecord, self.payload("tasks", deps=["T1", "T1"]))

    def test_ready_needs_explicit_owner_and_assignee(self):
        ready = self.payload("tasks", status="ready", latest_report_id=None)
        TaskRecord.model_validate(ready)
        for field in ("owner_profile_id", "assignee_profile_id"):
            with self.subTest(field=field):
                self.assert_invalid(TaskRecord, dict(ready, **{field: None}))

    def test_wait_kind_must_match_task_state_and_clear_on_resume(self):
        human = {"kind": "human", "request_id": "H-question"}
        children = {"kind": "children", "child_task_ids": ["T1"]}
        for status, wait in (("waiting_user", human), ("waiting_agent", children)):
            with self.subTest(status=status):
                TaskRecord.model_validate(self.payload("tasks", status=status, wait_ref=wait))
                self.assert_invalid(TaskRecord, self.payload("tasks", status=status, wait_ref=None))
        self.assert_invalid(TaskRecord, self.payload("tasks", status="waiting_user", wait_ref=children))
        self.assert_invalid(TaskRecord, self.payload("tasks", status="ready", wait_ref=human))
        self.assert_invalid(TaskRecord, self.payload("tasks", status="waiting_agent",
                                                    wait_ref={"kind": "children", "child_task_ids": ["T0"]}))

    def test_external_uncertainty_blocks_with_an_action_reference(self):
        value = self.payload("tasks", status="blocked", reason="external_state_unknown",
                             wait_ref={"kind": "external", "action_id": "X4"})
        TaskRecord.model_validate(value)
        self.assert_invalid(TaskRecord, dict(value, wait_ref=None))
        self.assert_invalid(TaskRecord, dict(value, reason="deps_pending"))
        self.assert_invalid(TaskRecord, self.payload("tasks", status="blocked", reason=None))

    def test_wait_union_rejects_empty_duplicate_and_mixed_references(self):
        adapter = TypeAdapter(WaitRef)
        for value in (
            {"kind": "children", "child_task_ids": []},
            {"kind": "children", "child_task_ids": ["T1", "T1"]},
            {"kind": "human", "request_id": "H1", "action_id": "X1"},
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                adapter.validate_python(value)

    def test_duplicate_artifact_ids_are_rejected_even_with_different_hashes(self):
        task = self.payload("tasks")
        ref = task["input_artifact_refs"][0]
        task["input_artifact_refs"].append(dict(ref, content_hash="sha256:" + "a" * 64))
        self.assert_invalid(TaskRecord, task)

    def test_suspension_preserves_attempt_and_requires_checkpoint_without_end_time(self):
        value = self.payload("attempts", state="suspended", ended_at=None, end_reason=None)
        AttemptRecord.model_validate(value)
        self.assert_invalid(AttemptRecord, dict(value, checkpoint_id=None))
        self.assert_invalid(AttemptRecord, dict(value, ended_at=value["updated_at"]))
        self.assert_invalid(AttemptRecord, dict(value, context_ref=None))
        self.assert_invalid(AttemptRecord, dict(value, started_at=None))

    def test_failure_before_attempt_start_is_representable(self):
        value = self.payload("attempts", state="failed", started_at=None, context_ref=None,
                             checkpoint_id=None, report_ids=[], end_reason="startup_failed")
        AttemptRecord.model_validate(value)
        self.assert_invalid(AttemptRecord, dict(value, state="succeeded"))
        self.assert_invalid(AttemptRecord, dict(value, end_reason=None))
        self.assert_invalid(AttemptRecord, dict(value, ended_at=None))

    def test_pending_attempt_cannot_claim_prior_execution(self):
        value = self.payload("attempts", state="pending", started_at=None, ended_at=None,
                             end_reason=None, context_ref=None, checkpoint_id=None, report_ids=[])
        AttemptRecord.model_validate(value)
        self.assert_invalid(AttemptRecord, dict(value, started_at=value["created_at"]))
        self.assert_invalid(AttemptRecord, dict(value, report_ids=["R0"]))

    def test_run_can_end_before_start_only_for_startup_failure_or_cancellation(self):
        value = self.payload("runs", state="ended", started_at=None, end_reason="startup_failed")
        RunRecord.model_validate(value)
        RunRecord.model_validate(dict(value, end_reason="cancelled"))
        self.assert_invalid(RunRecord, dict(value, end_reason="completed"))
        self.assert_invalid(RunRecord, dict(value, state="crashed"))
        self.assert_invalid(RunRecord, dict(value, ended_at=None))

    def test_prepared_and_running_runs_do_not_claim_end_times(self):
        value = self.payload("runs", state="prepared", started_at=None, ended_at=None, end_reason=None)
        RunRecord.model_validate(value)
        self.assert_invalid(RunRecord, dict(value, state="running"))
        self.assert_invalid(RunRecord, dict(value, ended_at=value["updated_at"]))

    def test_execution_timestamps_must_fit_snapshot_order(self):
        for model, collection in ((AttemptRecord, "attempts"), (RunRecord, "runs")):
            with self.subTest(model=model.__name__):
                self.assert_invalid(model, self.payload(collection, started_at="2026-11-15T09:59:59Z"))
                self.assert_invalid(model, self.payload(collection, ended_at="2026-11-15T11:00:00Z"))
                self.assert_invalid(model, self.payload(collection, updated_at="2026-11-15T09:59:59Z"))

    def test_resume_cursor_does_not_replay_observed_or_completed_actions(self):
        cursor = dict(step=0, phase="before_action", pending_action_ids=["X4"],
                      pending_tool_call_ids=["TC-X4"], observed_tool_call_ids=[])
        ResumeCursor.model_validate(cursor)
        self.assert_invalid(ResumeCursor, dict(cursor, observed_tool_call_ids=["TC-X4"]))
        self.assert_invalid(ResumeCursor, dict(cursor, phase="before_model"))
        self.assert_invalid(Checkpoint, self.payload("checkpoints", 2, completed_action_ids=["X4"]))

    def test_checkpoint_needs_unique_delivery_keys_and_records_for_waited_children(self):
        value = self.payload("checkpoints", 1)
        value["pending_delegations"][1]["delivery_key"] = value["pending_delegations"][0]["delivery_key"]
        self.assert_invalid(Checkpoint, value)
        self.assert_invalid(Checkpoint, self.payload("checkpoints", 1, pending_delegations=[]))
        self.assert_invalid(Checkpoint, self.payload("checkpoints", applied_message_ids=["M1", "M1"]))

    def test_strict_integers_and_boolean_flags_do_not_coerce(self):
        for bad in (True, -1, "1", 1.0):
            with self.subTest(bad=bad):
                self.assert_invalid(Quantity, dict(tokens=bad, llm_calls=1, tool_calls=0))
        self.assert_invalid(AttemptRecord, self.payload("attempts", number=True))
        self.assert_invalid(Checkpoint, self.payload("checkpoints", acceptance_failures=False))
        self.assert_invalid(PendingDelegation, dict(child_task_id="T1", tool_call_id=None,
                                                   delivery_key="D1", delivered=1))

    def test_root_budget_identity_is_not_a_child_scope(self):
        self.assert_invalid(BudgetScope, self.payload("budget_scopes", parent_scope_id="B9"))
        self.assert_invalid(BudgetScope, self.payload("budget_scopes", 1, parent_scope_id=None))
        self.assert_invalid(BudgetScope, self.payload("budget_scopes", 1, parent_scope_id="B1"))

    def test_actual_overruns_are_preserved_instead_of_rejected(self):
        value = self.payload("budget_leases")
        value["actual"]["tokens"] = value["reserved"]["tokens"] + 100
        self.assertEqual(BudgetLease.model_validate(value).actual.tokens, value["actual"]["tokens"])
        scope = self.payload("budget_scopes")
        scope["active_used_ms"] = scope["active_limit_ms"] + 1
        self.assertEqual(BudgetScope.model_validate(scope).active_used_ms, scope["active_used_ms"])

    def test_reserved_lease_cannot_claim_dispatch_or_actual_usage(self):
        value = self.payload("budget_leases", status="reserved", actual=None, dispatched_at=None,
                             settled_at=None, evidence_ref=None, reason=None)
        BudgetLease.model_validate(value)
        self.assert_invalid(BudgetLease, dict(value, dispatched_at=value["created_at"]))
        self.assert_invalid(BudgetLease, dict(value, actual=dict(tokens=0, llm_calls=0, tool_calls=0)))

    def test_unknown_usage_retains_reservation_and_has_no_settlement(self):
        value = self.payload("budget_leases", status="unknown", actual=None,
                             settled_at=None, reason="provider_timeout")
        lease = BudgetLease.model_validate(value)
        self.assertEqual(lease.reserved.tokens, value["reserved"]["tokens"])
        self.assert_invalid(BudgetLease, dict(value, actual=dict(tokens=0, llm_calls=0, tool_calls=0)))
        self.assert_invalid(BudgetLease, dict(value, settled_at=value["updated_at"]))
        self.assert_invalid(BudgetLease, dict(value, dispatched_at=None))
        self.assert_invalid(BudgetLease, dict(value, reason=None))

    def test_zero_token_failure_still_consumes_a_call(self):
        value = self.payload("budget_leases", actual=dict(tokens=0, llm_calls=1, tool_calls=0))
        BudgetLease.model_validate(value)
        self.assert_invalid(BudgetLease, dict(value, actual=dict(tokens=0, llm_calls=0, tool_calls=0)))
        self.assert_invalid(BudgetLease, dict(value, evidence_ref=None))
        self.assert_invalid(BudgetLease, dict(value, dispatched_at=None))

    def test_release_requires_proven_unsent_zero_usage(self):
        value = self.payload("budget_leases", status="released", dispatched_at=None,
                             actual=dict(tokens=0, llm_calls=0, tool_calls=0),
                             reason="verified_not_dispatched", evidence_ref={"kind": "receipt", "id": "RC-unsent"})
        BudgetLease.model_validate(value)
        for update in (
            {"actual": None}, {"evidence_ref": None}, {"reason": None}, {"settled_at": None},
            {"dispatched_at": value["created_at"]},
            {"actual": dict(tokens=0, llm_calls=1, tool_calls=0)},
        ):
            with self.subTest(update=update):
                self.assert_invalid(BudgetLease, dict(value, **update))

    def test_call_reservation_and_lease_time_order_are_required(self):
        self.assert_invalid(BudgetLease, self.payload("budget_leases", reserved=dict(tokens=1000, llm_calls=0, tool_calls=1)))
        self.assert_invalid(BudgetLease, self.payload("budget_leases", kind="tool"))
        self.assert_invalid(BudgetLease, self.payload("budget_leases", dispatched_at="2026-11-15T09:59:59Z"))
        self.assert_invalid(BudgetLease, self.payload("budget_leases", settled_at="2026-11-15T11:00:00Z"))
        self.assert_invalid(BudgetLease, self.payload("budget_leases", dispatched_at="2026-11-15T10:00:07Z",
                                                     settled_at="2026-11-15T10:00:06Z"))

    def test_snapshot_values_are_immutable(self):
        checkpoint = Checkpoint.model_validate(self.payload("checkpoints"))
        with self.assertRaises(ValidationError):
            checkpoint.run_id = "replacement"
        with self.assertRaises(TypeError):
            checkpoint.loop_state["repetitions"] = []
        self.assertIsInstance(checkpoint.applied_message_ids, tuple)


if __name__ == "__main__":
    unittest.main()
