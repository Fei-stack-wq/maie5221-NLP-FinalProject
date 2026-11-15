"""Contract failures that must not become approvals or successful actions."""
from __future__ import annotations

import copy
import json
import unittest

from miniagent.contracts.base import canonical_hash
from miniagent.contracts.human import (
    ActionRecord, Continuation, HumanReply, HumanRequest,
    action_intent_digest, validate_approval, validate_reply,
)


def at(seconds: int) -> str:
    return f"2026-11-15T10:{seconds // 60:02d}:{seconds % 60:02d}Z"


def action_data(status: str = "prepared", **changes) -> dict:
    executed = status in {"running", "succeeded", "failed", "unknown"}
    data = {
        "created_at": at(0), "revision": 3 if executed else 1,
        "updated_at": at(30) if executed else at(0),
        "action_id": "X1", "task_id": "T1", "attempt_id": "AT1",
        "tool_call_id": "TC1", "operation": "apply_patch", "effect": "external_write",
        "args": {"patch": "F1", "note": "只改启动配置"},
        "expected_resources": [{"resource_id": "config", "state": "present",
                                "version": "sha256:" + "0" * 64}],
        "scope": {"project_id": "P1", "root_task_id": "T0"},
        "constraints_ref": {"id": "CON1", "version": 1},
        "authorization_ref": "AUTH1" if executed else None,
        "approval_request_id": "H1", "status": status,
        "execution_run_id": "RUN1" if executed else None,
        "lease_id": "L1" if executed else None,
        "receipt_ref": {"kind": "receipt", "id": "RC1"} if status == "succeeded" else None,
        "reason": "stopped" if status in {"failed", "unknown", "cancelled"} else None,
    }
    data.update(changes)
    data["args_hash"] = canonical_hash(data["args"])
    data["intent_digest"] = action_intent_digest(**{
        key: data[key] for key in (
            "operation", "effect", "args_hash", "expected_resources", "scope", "constraints_ref"
        )
    })
    return data


def request_data(kind: str = "question", status: str = "pending", **changes) -> dict:
    action = action_data()
    data = {
        "created_at": at(10), "revision": 2 if status != "pending" else 1,
        "updated_at": at(20) if status != "pending" else at(10),
        "request_id": "H1", "task_id": "T1", "attempt_id": "AT1",
        "kind": kind, "prompt": "请确认", "choices": [],
        "requested_from": {"kind": "user", "id": "U1"},
        "action_id": "X1" if kind == "approval" else None,
        "action_digest": action["intent_digest"] if kind == "approval" else None,
        "status": status, "expires_at": at(60),
        "reply_id": "RP1" if status == "answered" else None,
        "continuation": {
            "kind": "authorize_action" if kind == "approval" else "answer_question",
            "tool_call_id": "TC1",
        },
    }
    data.update(changes)
    return data


def reply_data(kind: str = "clarification", **changes) -> dict:
    data = {
        "created_at": at(20), "reply_id": "RP1", "request_id": "H1",
        "actor": {"kind": "user", "id": "U1"}, "kind": kind,
        "answer": "Run A" if kind == "clarification" else None,
        "decision": None if kind == "clarification" else "allow", "replied_at": at(20),
    }
    data.update(changes)
    return data


class HumanRecordTests(unittest.TestCase):
    def test_explicit_nullable_fields_and_json_array_round_trip(self):
        request = HumanRequest(**request_data(choices=["A", "B"]))
        self.assertEqual(request.choices, ("A", "B"))
        dumped = json.loads(request.model_dump_json())
        self.assertEqual(dumped["choices"], ["A", "B"])
        self.assertIsNone(dumped["action_id"])
        self.assertEqual(HumanRequest.model_validate_json(request.model_dump_json()), request)
        for field in ("action_id", "action_digest", "expires_at", "reply_id", "choices"):
            with self.subTest(field=field):
                data = request_data()
                del data[field]
                with self.assertRaises(ValueError):
                    HumanRequest(**data)
        with self.assertRaises(ValueError):
            Continuation(kind="answer_question")

    def test_request_kind_controls_binding_and_continuation(self):
        invalid = [
            request_data(action_id="X1"),
            request_data(action_digest="sha256:" + "0" * 64),
            request_data("approval", action_id=None),
            request_data("approval", action_digest=None),
            request_data(continuation={"kind": "authorize_action", "tool_call_id": None}),
            request_data("approval", continuation={"kind": "answer_question", "tool_call_id": None}),
        ]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                HumanRequest(**data)

    def test_request_status_requires_exact_reply_presence(self):
        for data in [request_data(status="answered", reply_id=None),
                     request_data(reply_id="RP1"),
                     request_data(status="cancelled", reply_id="RP1")]:
            with self.subTest(status=data["status"]), self.assertRaises(ValueError):
                HumanRequest(**data)

    def test_expiry_and_record_times(self):
        invalid = [request_data(expires_at=at(10)), request_data(expires_at=at(9)),
                   request_data(updated_at=at(9)), request_data(updated_at=at(60)),
                   request_data(status="expired", expires_at=None),
                   request_data(status="expired", updated_at=at(59))]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                HumanRequest(**data)
        HumanRequest(**request_data(status="expired", updated_at=at(60)))
        HumanRequest(**request_data(expires_at=None))

    def test_requests_and_replies_are_user_only(self):
        for kind in ("profile", "service"):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    HumanRequest(**request_data(requested_from={"kind": kind, "id": "U1"}))
                with self.assertRaises(ValueError):
                    HumanReply(**reply_data(actor={"kind": kind, "id": "U1"}))

    def test_reply_union_conditions(self):
        HumanReply(**reply_data())
        HumanReply(**reply_data("approval", decision="deny"))
        for data in [reply_data(answer=None), reply_data(answer="  "),
                     reply_data(decision="allow"), reply_data("approval", decision=None),
                     reply_data("approval", decision=True)]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                HumanReply(**data)
        for field in ("answer", "decision"):
            data = reply_data()
            del data[field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                HumanReply(**data)

    def test_reply_event_cannot_postdate_record_creation(self):
        with self.assertRaises(ValueError):
            HumanReply(**reply_data(replied_at=at(21)))
        HumanReply(**reply_data(created_at=at(21)))

    def test_strict_extra_and_immutable_snapshots(self):
        for changes in ({"revision": True}, {"request_id": 1}, {"unrecognized": True},
                        {"record_type": "ActionRecord"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                HumanRequest(**request_data(**changes))
        request = HumanRequest(**request_data())
        with self.assertRaises(ValueError):
            request.status = "answered"


class ReplyBindingTests(unittest.TestCase):
    def test_pending_and_answered_accept_valid_reply_including_denial(self):
        for status in ("pending", "answered"):
            validate_reply(HumanRequest(**request_data(status=status)), HumanReply(**reply_data()))
            validate_reply(HumanRequest(**request_data("approval", status)),
                           HumanReply(**reply_data("approval", decision="deny")))

    def test_wrong_identity_id_or_kind_is_not_a_valid_reply(self):
        request = HumanRequest(**request_data())
        for data in [reply_data(request_id="H2"),
                     reply_data(actor={"kind": "user", "id": "U2"}),
                     reply_data("approval")]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                validate_reply(request, HumanReply(**data))

    def test_cancelled_expired_and_replaced_reply_rejected(self):
        reply = HumanReply(**reply_data())
        for data in [request_data(status="cancelled"),
                     request_data(status="expired", updated_at=at(60)),
                     request_data(status="answered", reply_id="RP2")]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                validate_reply(HumanRequest(**data), reply)

    def test_reply_time_window_and_answered_snapshot(self):
        request = HumanRequest(**request_data())
        for t in (9, 60, 61):
            with self.subTest(time=t), self.assertRaises(ValueError):
                validate_reply(request, HumanReply(**reply_data(created_at=at(t), replied_at=at(t))))
        validate_reply(request, HumanReply(**reply_data(created_at=at(59), replied_at=at(59))))
        with self.assertRaises(ValueError):
            validate_reply(HumanRequest(**request_data(status="answered", updated_at=at(19))),
                           HumanReply(**reply_data()))


class ActionRecordTests(unittest.TestCase):
    def test_all_execution_states_have_valid_canonical_examples(self):
        for status in ("prepared", "running", "succeeded", "failed", "unknown", "cancelled"):
            with self.subTest(status=status):
                action = ActionRecord(**action_data(status))
                self.assertEqual(ActionRecord.model_validate_json(action.model_dump_json()), action)

    def test_nullable_fields_still_required(self):
        for field in ("tool_call_id", "authorization_ref", "approval_request_id", "execution_run_id",
                      "lease_id", "receipt_ref", "reason"):
            data = action_data()
            del data[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                ActionRecord(**data)

    def test_args_or_intent_tampering_is_rejected(self):
        data = action_data()
        data["args"]["patch"] = "different-patch"
        with self.assertRaises(ValueError):
            ActionRecord(**data)
        for field, value in (("operation", "delete_file"), ("effect", "read"),
                             ("scope", {"project_id": "other", "root_task_id": "T0"}),
                             ("constraints_ref", {"id": "CON1", "version": 2}),
                             ("intent_digest", "sha256:" + "0" * 64)):
            data = action_data()
            data[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                ActionRecord(**data)

    def test_digest_sorts_resources_and_supports_absence(self):
        present = action_data()["expected_resources"][0]
        absent = {"resource_id": "new-file", "state": "absent"}
        first = action_data(expected_resources=[present, absent])
        second = action_data(expected_resources=[absent, present])
        self.assertEqual(first["intent_digest"], second["intent_digest"])
        action = ActionRecord(**first)
        self.assertEqual(action.expected_resources[1].state, "absent")
        bad = copy.deepcopy(first)
        bad["expected_resources"].append(present)
        with self.assertRaises(ValueError):
            ActionRecord(**bad)
        with self.assertRaises(ValueError):
            action_intent_digest(**{key: bad[key] for key in (
                "operation", "effect", "args_hash", "expected_resources", "scope", "constraints_ref"
            )})

    def test_nested_float_is_rejected_even_if_a_hash_is_supplied(self):
        data = action_data()
        data["args"] = {"nested": [{"ratio": 0.5}]}
        with self.assertRaises(ValueError):
            ActionRecord(**data)

    def test_nested_args_are_read_only(self):
        action = ActionRecord(**action_data(args={"nested": {"paths": ["a", "b"]}}))
        with self.assertRaises(TypeError):
            action.args["nested"]["paths"] = ("x",)
        with self.assertRaises(TypeError):
            action.args["nested"]["paths"][0] = "x"

    def test_executed_states_require_authorization_run_and_lease(self):
        for status in ("running", "succeeded", "failed", "unknown"):
            for field in ("authorization_ref", "execution_run_id", "lease_id"):
                with self.subTest(status=status, field=field), self.assertRaises(ValueError):
                    ActionRecord(**action_data(status, **{field: None}))

    def test_unexecuted_states_cannot_claim_execution(self):
        for status in ("prepared", "cancelled"):
            for field, value in (("execution_run_id", "RUN1"), ("lease_id", "L1"),
                                 ("receipt_ref", {"kind": "receipt", "id": "RC1"})):
                with self.subTest(status=status, field=field), self.assertRaises(ValueError):
                    ActionRecord(**action_data(status, **{field: value}))

    def test_external_write_requires_resource_states_on_execution(self):
        ActionRecord(**action_data(expected_resources=[]))
        with self.assertRaises(ValueError):
            ActionRecord(**action_data("running", expected_resources=[]))
        ActionRecord(**action_data("running", effect="private_write", expected_resources=[]))

    def test_result_conditions_do_not_confuse_approval_with_execution(self):
        for data in [action_data("succeeded", receipt_ref=None),
                     action_data("succeeded", receipt_ref={"kind": "reply", "id": "RP1"}),
                     action_data("running", receipt_ref={"kind": "receipt", "id": "RC1"})]:
            with self.subTest(status=data["status"]), self.assertRaises(ValueError):
                ActionRecord(**data)
        for status in ("failed", "unknown", "cancelled"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                ActionRecord(**action_data(status, reason=None))


class ApprovalBindingTests(unittest.TestCase):
    def setUp(self):
        self.request = HumanRequest(**request_data("approval", "answered"))
        self.reply = HumanReply(**reply_data("approval"))

    def test_prepared_and_executed_action_accept_matching_chain(self):
        for status in ("prepared", "running", "succeeded"):
            with self.subTest(status=status):
                validate_approval(ActionRecord(**action_data(status)), self.request, self.reply)

    def test_answered_deny_and_unanswered_allow_are_not_approval(self):
        action = ActionRecord(**action_data())
        with self.assertRaises(ValueError):
            validate_approval(action, self.request, HumanReply(**reply_data("approval", decision="deny")))
        with self.assertRaises(ValueError):
            validate_approval(action, HumanRequest(**request_data("approval")), self.reply)
        with self.assertRaises(ValueError):
            validate_approval(action, HumanRequest(**request_data(status="answered")),
                              HumanReply(**reply_data()))

    def test_exact_action_task_attempt_digest_and_tool_call_binding(self):
        action = ActionRecord(**action_data())
        cases = [("action_id", "X2"), ("task_id", "T2"), ("attempt_id", "AT2"),
                 ("action_digest", "sha256:" + "0" * 64),
                 ("continuation", {"kind": "authorize_action", "tool_call_id": "TC2"})]
        for field, value in cases:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_approval(action,
                                  HumanRequest(**request_data("approval", "answered", **{field: value})),
                                  self.reply)
        with self.assertRaises(ValueError):
            validate_approval(ActionRecord(**action_data(approval_request_id="H2")),
                              self.request, self.reply)

    def test_approval_must_follow_intent_and_precede_execution_snapshot(self):
        with self.assertRaises(ValueError):
            validate_approval(ActionRecord(**action_data(created_at=at(11), updated_at=at(11))),
                              self.request, self.reply)
        with self.assertRaises(ValueError):
            validate_approval(ActionRecord(**action_data("succeeded", updated_at=at(19))),
                              self.request, self.reply)


if __name__ == "__main__":
    unittest.main()
