"""Diagnostics explain observed failures without rewriting historical truth."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import time
import unittest
import uuid
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.provider_http import HTTPResult, ProviderError
from miniagent.studio_diagnostics import diagnose
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_service import finished, request
from tests.test_studio_crew import roster


def task(identity="root", status="unverified", parent=None):
    return {"task_id": identity, "status": status, "parent_id": parent}


def report(verdict="unverified", spec=None, identity="root"):
    return {"task_id": identity, "spec": spec,
            "report": {"verdict": verdict, "reason": "check explanation"}}


def event(kind, identity="root", **fields):
    return {"kind": kind, "task_id": identity, **fields}


class StudioDiagnosticsTest(unittest.TestCase):
    def test_resumed_member_file_recovers_only_the_matching_failure(self):
        tasks = [task(status="running"), {**task("first", "failed", "root"), "assignee_profile_id": "research"},
                 task("second", "unverified", "root")]
        events = [event("task_end", "first", status="error", error="[BLOCKED] missing source"),
                  event("task_start", agent="lead"), event("step_start", step=27, phase="coordinate")]
        failure = {"kind": "member_report", "role": "research", "task_id": "first",
                   "published": False, "handoff": {"deliverable_paths": ["research.md"]}}
        retry = {"kind": "assignment", "recipient": "research", "task_id": "second",
                 "resumed_private_draft": True, "recover_from": "research"}
        success = {"kind": "member_report", "role": "research", "task_id": "second",
                   "published": True, "handoff": {"deliverable_paths": ["research.md"]}}
        for assignment in (retry, {**retry, "recovered_task_id": "first"}):
            result = diagnose(tasks, events, [], active=True, room_events=[failure, assignment, success])
            self.assertEqual((result["execution_status"], result["acceptance_status"]), ("running", "pending"))
            self.assertEqual(result["current_activity"][0]["role"], "lead")
            issue, = result["issues"]
            self.assertEqual((issue["scope"], issue["role"], issue["resolved_by"], issue["severity"]),
                             ("member", "research", "second", "warning"))
            self.assertTrue(issue["resolved"])
            finished = diagnose([{**tasks[0], "status": "unverified"}, *tasks[1:]],
                                [*events, event("task_end", status="unverified")], [report()],
                                room_events=[failure, assignment, success])
            self.assertEqual(finished["execution_status"], "finished")
        for records in ([failure, success], [failure, {**retry, "recovered_task_id": "different"}, success],
                        [failure, retry, {**success, "task_id": "unrelated"}],
                        [failure, retry, {**success, "handoff": {"deliverable_paths": ["different.md"]}}],
                        [failure, retry, {**success, "role": "unrelated"}],
                        [failure, retry, {**success, "published": False}]):
            with self.subTest(records=records):
                issue, = diagnose(tasks, events, [], active=True, room_events=records)["issues"]
                self.assertEqual(issue["scope"], "member")
                self.assertFalse(issue.get("resolved"))
                self.assertEqual(issue["severity"], "error")

    def test_diagnoses_only_host_confirmed_blocked_handoff_before_writing(self):
        tasks = [task(status="failed"), task("child", "failed", "root")]
        identity = {"root_id": "root", "attempt_id": "a", "run_id": "r"}
        work = event("step_start", "child", **identity, phase="work", step=1, offered_tools=["write_file"])
        closing = event("step_start", "child", **identity, phase="handoff", step=9, offered_tools=[])
        child_end = event("task_end", "child", **identity, status="error")
        root_end = event("task_end", status="error", error="[BLOCKED] 写文件时被系统拒绝")
        evidence = {"stop_reason": "连续七轮没有新的文件、来源或检查结果", "missing_required_files": ["notes/a.md"]}
        receipt = {"kind": "member_report", "task_id": "child", "role": "writer", "status": "error",
                   "published": False, "handoff": {"evidence": evidence}, "text": "我是没有工具的角色"}
        events = [work, closing, {**closing, "step": 10}, child_end, root_end]
        drafts = [{"task_id": "child", "role": "writer"}]
        result = diagnose(tasks, events, [], room_events=[receipt], recovery_candidates=drafts)
        issue = result["issues"][0]
        self.assertEqual(issue["code"], "member_handoff_before_write")
        self.assertEqual(issue["missing_required_files"], ["notes/a.md"])
        self.assertEqual((issue["task_id"], issue["attempt_id"], issue["step"]), ("child", "a", 9))
        self.assertIn("工作阶段提供过", issue["message"])
        self.assertIn("选择这个成员的草稿", issue["suggestion"])
        self.assertEqual(result["execution_status"], "failed")
        result = diagnose(tasks, [work, {**work, "phase": "deliver", "step": 8}, closing, child_end, root_end],
                          [], room_events=[receipt])
        self.assertEqual(result["issues"][0]["code"], issue["code"])
        self.assertIn("专门写入", result["issues"][0]["message"])
        self.assertNotIn("选择这个成员的草稿", result["issues"][0]["suggestion"])

        write_failure = event("tool_result", "child", **identity, operation="write_file", ok=False)
        for trace, journal, incomplete in [
            ([work, write_failure, closing, child_end, root_end], [receipt], False),
            ([work, {**closing, "attempt_id": "old"}, child_end, root_end], [receipt], False),
            (events, [dict(receipt, handoff={})], False),
            (events, [receipt, dict(receipt, status="completed", published=True)], False),
            (events, [receipt], True),
            ([*events[:-1], event("task_end",status="completed")], [receipt], False),
            ([*events, event("studio_error", error_code="OSError")], [receipt], False),
        ]:
            with self.subTest(trace=trace, journal=journal, incomplete=incomplete):
                codes = [issue["code"] for issue in diagnose(tasks, trace, [], room_events=journal,
                                                              incomplete=incomplete)["issues"]]
                self.assertNotIn("member_handoff_before_write", codes)

    def test_historical_402_receipts_explain_balance_and_verified_recoverable_drafts(self):
        rows = [task(status="failed"), task("risk", "failed", "root")]
        events = []
        for identity, attempt in [("risk", "risk-attempt"), ("root", "lead-attempt")]:
            common = {"attempt_id":attempt,"run_id":attempt + "-run"}
            events.extend([
                event("step_start",identity,step=25,provider_config={"provider":"deepseek"},**common),
                event("provider_error",identity,step=25,code="http_error",http_status=402,
                      retryable=False,request_attempts=1,**common),
                event("task_end",identity,status="error",error="Provider returned an HTTP error",**common)])
        before = copy.deepcopy((rows,events))
        result = diagnose(rows,events,[],recovery_candidates=[{"task_id":"risk","role":"risk","objective":"private objective"}])
        self.assertEqual(result["execution_status"],"failed")
        self.assertEqual(len(result["issues"]),2)
        for issue in result["issues"]:
            self.assertEqual(issue["code"],"provider_balance_exhausted")
            self.assertEqual(issue["title"],"DeepSeek 账户额度不足")
            self.assertIn("本轮模型调用受阻",issue["message"])
            self.assertFalse(issue["resolved"])
            self.assertFalse(issue["retryable"])
            self.assertTrue(issue["retry_requires_available_balance"])
            self.assertEqual(issue["recoverable_drafts"],[{"task_id":"risk","role":"risk"}])
            self.assertIn("1 份可接续成员草稿",issue["suggestion"])
        self.assertEqual((rows,events),before)

    def test_new_balance_code_does_not_require_legacy_start_metadata(self):
        result = diagnose([task(status="running")],[event("provider_error",code="insufficient_balance",
            http_status=402,retryable=False,request_attempts=0)],[],active=True)
        self.assertEqual(result["execution_status"],"running")
        self.assertEqual(result["issues"][0]["title"],"DeepSeek 账户额度不足")
        self.assertEqual(result["issues"][0]["request_attempts"],0)
        self.assertEqual(result["issues"][0]["recoverable_drafts"],[])
        self.assertNotIn("份可接续",result["issues"][0]["suggestion"])

    def test_later_successful_attempt_retains_402_only_as_history(self):
        events = [event("provider_error",code="insufficient_balance",http_status=402,
                        attempt_id="old",run_id="old-run"),
                  event("task_end",status="error",attempt_id="old",run_id="old-run"),
                  event("step_start",step=1,attempt_id="new",run_id="new-run"),
                  event("step",step=1,finish_reason="stop",attempt_id="new",run_id="new-run"),
                  event("task_end",status="completed",attempt_id="new",run_id="new-run")]
        result = diagnose([task(status="completed")],events,[report("passed",{})])
        self.assertEqual(result["execution_status"],"finished")
        issue, = result["issues"]
        self.assertTrue(issue["resolved"])
        self.assertEqual(issue["severity"],"warning")
        self.assertFalse(issue["retry_requires_available_balance"])
        self.assertIn("后续请求已成功",issue["title"])
        # Recovery does not erase a subsequent independent execution failure.
        events.append(event("task_end",status="error",error="tool failed",attempt_id="new",run_id="new-run"))
        result = diagnose([task(status="failed")],events,[])
        self.assertEqual(result["execution_status"],"failed")
        self.assertEqual(result["issues"][-1]["code"],"execution_failed")

    def test_other_tasks_and_late_old_attempt_responses_do_not_resolve_balance_failure(self):
        for response in [event("step","other",finish_reason="stop",attempt_id="a",run_id="r"),
                         event("step",finish_reason="stop",attempt_id="older",run_id="older-run")]:
            with self.subTest(response=response):
                result = diagnose([task(status="failed")],[
                    event("provider_error",code="insufficient_balance",http_status=402,attempt_id="a",run_id="r"),
                    response,event("task_end",status="error",attempt_id="a",run_id="r")],[])
                self.assertFalse(result["issues"][0]["resolved"])
                self.assertEqual(result["execution_status"],"failed")
        result = diagnose([{**task(status="failed"),"current_attempt_id":"new"}],[
            event("step_start",attempt_id="old",run_id="old-run"),
            event("task_end",status="error",attempt_id="old",run_id="old-run"),
            event("provider_error",code="insufficient_balance",http_status=402,attempt_id="new",run_id="new-run"),
            event("step_start",attempt_id="old",run_id="old-run"),
            event("step",finish_reason="stop",attempt_id="old",run_id="old-run"),
            event("task_end",status="error",attempt_id="new",run_id="new-run")],[])
        self.assertFalse(result["issues"][0]["resolved"])

    def test_project_delivery_does_not_claim_child_balance_recovered(self):
        result=diagnose([task(status="completed"),task("child","failed","root")],[
            event("provider_error","child",code="insufficient_balance",http_status=402),
            event("task_end","child",status="error"),event("task_end",status="completed")],[report("passed",{})])
        self.assertEqual(result["execution_status"],"finished")
        issue,=result["issues"]
        self.assertEqual(issue["severity"],"warning")
        self.assertTrue(issue["superseded_by_delivery"])
        self.assertFalse(issue["resolved"])
        self.assertTrue(issue["retry_requires_available_balance"])

    def test_unrelated_http_errors_do_not_claim_deepseek_balance(self):
        for status, provider in [(401,"deepseek"),(429,"deepseek"),(402,"other")]:
            with self.subTest(status=status,provider=provider):
                result=diagnose([task(status="failed")],[
                    event("provider_error",provider=provider,code="http_error",http_status=status),
                    event("task_end",status="error",error="Provider returned an HTTP error")],[])
                self.assertEqual(result["issues"][0]["code"],"execution_failed")
        result = diagnose([task(status="failed")],[
            event("step_start",provider_config={"provider":"deepseek","official_api":False}),
            event("provider_error",code="http_error",http_status=402),
            event("task_end",status="error",error="HTTP error")],[])
        self.assertEqual(result["issues"][0]["code"],"execution_failed")

    def test_fresh_team_balance_failure_preserves_draft_and_stops_lead_http_request(self):
        root = make_temp_dir("studio-balance")
        secret = "private-balance-key"
        service = StudioService(root / "runs",Settings(api_key=secret))
        def response(call_id, name, arguments):
            return HTTPResult({"choices":[{"message":{"role":"assistant","content":"","reasoning_content":"",
                "tool_calls":[{
                "id":call_id,"type":"function","function":{"name":name,"arguments":json.dumps(arguments)}}]},
                "finish_reason":"tool_calls"}],"usage":{"prompt_tokens":12,"completion_tokens":5}},1)
        transport = [response("delegate","delegate_task",{"assignee":"risk","objective":"Write a recoverable draft.",
                     "deliverable_paths":["draft.md"],"remaining_roles":[]}),
                     response("write","write_file",{"path":"draft.md","content":"# Work in progress\nEvidence retained."}),
                     ProviderError("Provider returned an HTTP error",code="http_error",status_code=402,attempts=1)]
        try:
            with patch("miniagent.providers.post_json",side_effect=transport) as post:
                run = service.start(request("team",conversation=roster("lead","risk"),
                    objective="Write a report with review.",acceptance_files=["draft.md"],limits={"max_steps":15}))
                detail = finished(service,run["id"])
            self.assertEqual(post.call_count,3)
            self.assertEqual(detail["run"]["status"],"failed")
            errors = [e for e in detail["events"] if e["kind"]=="provider_error"]
            self.assertEqual([e["request_attempts"] for e in errors],[1,0])
            self.assertTrue(all(e["code"]=="insufficient_balance" and not e["retryable"] for e in errors))
            candidates = detail["conversation"]["recovery_candidates"]
            self.assertEqual([c["role"] for c in candidates],["risk"])
            issue = detail["diagnostics"]["issues"][0]
            self.assertEqual(issue["recoverable_drafts"][0]["task_id"],candidates[0]["task_id"])
            self.assertIn("可接续成员草稿",issue["suggestion"])
            self.assertNotIn(secret,json.dumps(detail))
            # Merely reading an old turn must not offer a later turn's drafts.
            conversation = {**detail["conversation"],"head_run_id":"a-later-run"}
            with patch.object(service,"_conversation",return_value=conversation):
                older = service.detail(run["id"])
            self.assertTrue(all(not i.get("recoverable_drafts") for i in older["diagnostics"]["issues"]))
        finally:
            service.close()
            cleanup(root)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the existing GUI renderer")
    def test_gui_shows_balance_cause_during_shutdown_and_resolved_history_afterwards(self):
        source = (Path(__file__).parents[1]/"miniagent/web/app.js").read_text()
        renderer = source[source.index("function diagnosis(detail){"):source.index("const roomHistory =")]
        labels = next(line for line in source.splitlines() if line.startswith("const labels ="))
        current = diagnose([task(status="running")],[event("provider_error",code="insufficient_balance",
            http_status=402,retryable=False,request_attempts=1)],[],active=True)
        historical = diagnose([task(status="completed")],[event("provider_error",code="insufficient_balance",http_status=402),
            event("step",finish_reason="stop"),event("task_end",status="completed")],[report("passed",{})])
        fixtures=[{"run":{"status":status},"diagnostics":value,"tasks":[]}
                  for status,value in [("running",current),("completed",historical)]]
        javascript = ("const runActive=r=>r.status==='running',runStatus=r=>r.status,endedWithoutChecks=()=>false;\n"
                      "const e=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');\n"
                      "const icon=()=>'',duration=()=>'',roleLabel=id=>id;\n" + labels + "\n" + renderer +
                      "console.log(JSON.stringify(" + json.dumps(fixtures,ensure_ascii=False) + ".map(diagnosis)));\n")
        result=subprocess.run([shutil.which("node")],input=javascript,text=True,capture_output=True,check=True)
        live,old=json.loads(result.stdout)
        headline, evidence = live.split("<summary>查看诊断记录与运行配置</summary>")
        self.assertIn("<h2>正在执行任务</h2>",headline)
        self.assertIn("运行中",headline)
        self.assertNotIn("额度不足",headline)
        self.assertIn("DeepSeek 账户额度不足",evidence)
        self.assertIn("HTTP 402",evidence)
        self.assertIn("重试需要可用",evidence)
        self.assertIn("后续请求已成功",old)
        self.assertNotIn("重试需要可用",old)

    def test_accepted_root_after_repair_retains_child_issue(self):
        result = diagnose([task(status="completed"), task("draft", "failed", parent="root")],
                          [event("task_end", "draft", status="failed", error="missing deliverable"),
                           event("task_end", status="completed")], [report("passed", {"id": "check"})])
        self.assertEqual(result["execution_status"], "finished")
        self.assertEqual(result["issues"][0]["task_id"], "draft")
        self.assertEqual(result["issues"][0]["severity"], "warning")

    def test_live_activity_exposes_runtime_phase(self):
        result = diagnose([], [event("step_start", step=4, phase="handoff")], [], active=True)
        self.assertEqual(result["current_activity"][0]["phase"], "handoff")

    def test_step_limit_is_distinct_from_model_output_truncation(self):
        events = [event("studio_configuration", configuration={"max_steps": 12, "max_output_tokens": 300000}),
                  event("step", step=12, finish_reason="tool_calls", completion_tokens=894),
                  event("task_end", status="limit_exceeded", error="超过最大步数 12")]
        result = diagnose([task(status="failed")], events, [])
        self.assertEqual(result["issues"][0]["code"], "step_limit")
        self.assertEqual(result["configuration"]["max_steps"], 12)
        self.assertFalse(any(issue["code"] == "output_truncated" for issue in result["issues"]))

    def test_other_runtime_limits_are_not_misreported_as_timeouts(self):
        for reason, code in [("检测到重复动作（T_LOOP）", "loop_limit"), ("超过动作总数上限", "action_limit"),
                             ("未知运行限制", "runtime_limit"), ("超过时间上限 3600s", "time_budget")]:
            with self.subTest(code=code):
                value = diagnose([task(status="failed")], [event("task_end", status="limit_exceeded", error=reason)], [])
                self.assertEqual(value["issues"][0]["code"], code)

    def test_cumulative_token_incident_explains_historical_prompt_and_output_totals(self):
        value = diagnose([task(status="failed")], [
            event("task_end", status="limit_exceeded", error="超过 token 预算 1000000",
                  prompt_tokens=1010961, completion_tokens=24388)
        ], [])
        issue = value["issues"][0]
        self.assertEqual(issue["code"], "token_budget")
        for count in ("1,010,961", "24,388", "1,035,349"):
            self.assertIn(count, issue["message"])

    def test_cancelled_task_is_not_presented_as_an_execution_error(self):
        result = diagnose([task(status="cancelled")],
                          [event("task_end", status="aborted", error="用户请求停止")], [])
        self.assertEqual(result["execution_status"], "cancelled")
        self.assertEqual(result["issues"], [])

    def test_complete_response_after_recovery_does_not_hide_later_step_limit(self):
        events = [event("step", step=1, finish_reason="length", attempt_id="a", run_id="r"),
                  event("step", step=2, finish_reason="tool_calls", attempt_id="a", run_id="r"),
                  event("task_end", status="limit_exceeded", error="超过最大步数 2", attempt_id="a", run_id="r")]
        result = diagnose([task(status="failed")], events, [])
        self.assertTrue(result["issues"][0]["resolved"])
        self.assertEqual(result["issues"][1]["code"], "step_limit")

    def test_original_incident_has_two_causes_without_upgrading_historical_status(self):
        tasks = [task(), task("root:s1", "failed", "root"), task("root:s2", "cancelled", "root")]
        events = [
            event("step", "root:s1", step=2, finish_reason="length", completion_tokens=4096,
                  latency_ms=22828.83, request_id="historical-request"),
            event("task_end", "root:s1", status="error", error="T_PROTOCOL"),
            event("subtask_end", "root:s2", status="blocked", error="上游未完成：s1"),
            event("task_end", status="unverified", error="未配置验收 (no-acceptance)"),
        ]
        reports = [report()]
        before = copy.deepcopy((tasks, events, reports))
        diagnostics = diagnose(tasks, events, reports)
        self.assertEqual(diagnostics["execution_status"], "failed")
        self.assertEqual(diagnostics["acceptance_status"], "not_configured")
        self.assertEqual({i["code"] for i in diagnostics["issues"]}, {"output_truncated", "no_acceptance"})
        truncated = diagnostics["issues"][0]
        self.assertEqual(truncated["completion_tokens"], 4096)
        self.assertFalse(truncated["resolved"])
        self.assertEqual(truncated["severity"], "error")
        # The old trace reports consumed tokens, not the configured ceiling.
        self.assertIsNone(truncated["max_output_tokens"])
        self.assertEqual(diagnostics["blocked_tasks"][0]["task_id"], "root:s2")
        self.assertEqual((tasks, events, reports), before)

    def test_new_recorded_output_limit_is_used_without_guessing_from_current_settings(self):
        events = [event("studio_configuration", configuration={"max_output_tokens": 1234}),
                  event("step", step=1, finish_reason="length", completion_tokens=1234),
                  event("task_end", status="error", error="truncated")]
        result = diagnose([task(status="failed")], events, [])
        self.assertEqual(result["issues"][0]["max_output_tokens"], 1234)

    def test_failed_acceptance_does_not_falsely_claim_execution_failed(self):
        result = diagnose([task(status="failed")],
                          [event("task_end", status="failed_acceptance", error="check failed")],
                          [report("failed", spec={"spec_id": "trusted-check"})])
        self.assertEqual(result["execution_status"], "finished")
        self.assertEqual(result["acceptance_status"], "failed")
        self.assertEqual([i["code"] for i in result["issues"]], ["acceptance_failed"])

    def test_missing_spec_does_not_turn_evaluator_error_into_no_acceptance(self):
        result = diagnose([task(status="failed")], [], [report("error")], incomplete=True)
        self.assertEqual(result["acceptance_status"], "error")
        self.assertEqual(result["execution_status"], "unknown")
        self.assertEqual(result["issues"][0]["code"], "acceptance_error")
        self.assertTrue(result["incomplete"])

    def test_parallel_activity_is_correlated_by_attempt_run_and_step(self):
        events = [
            event("plan_start", call_id="p1", attempt_id="root-a", run_id="root-r"),
            event("plan_usage", call_id="p1", attempt_id="root-a", run_id="root-r"),
            event("step_start", "a", step=1, attempt_id="a1", run_id="ra"),
            event("step_start", "b", step=1, attempt_id="b2", run_id="rb", ts=5, iso="2026-01-01T00:00:05Z"),
            event("step", "a", step=1, attempt_id="a1", run_id="ra"),
            # A late terminal from an old attempt cannot close b's new call.
            event("task_end", "b", attempt_id="b1", run_id="old", status="error"),
        ]
        result = diagnose([task(status="running")], events, [], active=True)
        self.assertEqual(len(result["current_activity"]), 1)
        self.assertEqual(result["current_activity"][0]["task_id"], "b")
        self.assertEqual(result["current_activity"][0]["phase"], "agent_step")
        self.assertEqual(result["current_activity"][0]["started_ts"], 5)
        events.append(event("provider_error", "b", step=1, attempt_id="b2", run_id="rb"))
        self.assertEqual(diagnose([], events, [], active=True)["current_activity"], [])

    def test_planning_error_matches_the_call_and_terminal_closes_unreturned_step(self):
        events = [event("plan_start", call_id="p1"), event("plan_start", call_id="p2"),
                  event("provider_error", call_id="p1"),
                  event("step_start", "child", step=1, attempt_id="a", run_id="r"),
                  event("task_end", "child", attempt_id="a", run_id="r", status="error")]
        current = diagnose([], events, [], active=True)["current_activity"]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["phase"], "planning")
        events.append(event("plan_usage", call_id="p2"))
        self.assertEqual(diagnose([], events, [], active=True)["current_activity"], [])

    def test_planner_truncation_is_explained_even_when_final_log_is_missing(self):
        result = diagnose([task(status="running")], [
            event("plan_usage", finish_reason="length", completion_tokens=4096, max_output_tokens=4096)
        ], [], incomplete=True)
        self.assertEqual(result["execution_status"], "failed")
        self.assertEqual(result["issues"][0]["code"], "output_truncated")
        self.assertIn("子任务未调度", result["issues"][0]["title"])

    def test_finished_child_does_not_prove_interrupted_root_finished(self):
        result = diagnose([task(status="running"), task("child", "completed", "root")],
                          [event("task_end", "child", status="completed")], [], incomplete=True)
        self.assertEqual(result["execution_status"], "unknown")
        self.assertEqual(result["current_activity"], [])

    def test_accepted_plan_after_truncation_is_a_resolved_historical_warning(self):
        events = [
            event("plan_usage", finish_reason="length", attempt_id="a", run_id="r"),
            event("plan_usage", finish_reason="stop", attempt_id="a", run_id="r"),
            {"kind": "plan", "root_id": "root", "root_attempt_id": "a", "root_run_id": "r"},
            event("task_end", status="completed", attempt_id="a", run_id="r"),
        ]
        result = diagnose([task(status="completed")], events, [report("passed", spec={})])
        self.assertEqual(result["execution_status"], "finished")
        self.assertEqual(result["acceptance_status"], "passed")
        self.assertEqual(len(result["issues"]), 1)
        issue = result["issues"][0]
        self.assertTrue(issue["resolved"])
        self.assertEqual(issue["severity"], "warning")
        self.assertNotIn("子任务未调度", issue["title"])

    def test_later_normal_response_without_accepted_plan_does_not_resolve_truncation(self):
        events = [event("plan_usage", finish_reason="length", attempt_id="a", run_id="r"),
                  event("plan_usage", finish_reason="stop", attempt_id="a", run_id="r"),
                  event("plan_invalid", attempt_id="a", run_id="r")]
        result = diagnose([task(status="running")], events, [])
        self.assertFalse(result["issues"][0]["resolved"])

    def test_conflicting_or_partially_missing_attempt_identity_does_not_claim_recovery(self):
        for first in ({"attempt_id": "a", "run_id": "r"}, {}):
            with self.subTest(first=first):
                events = [event("plan_usage", finish_reason="length", **first),
                          event("plan", attempt_id="other", run_id="other-run"),
                          event("task_end", status="completed", attempt_id="other", run_id="other-run")]
                result = diagnose([task(status="completed")], events, [report("passed", spec={})])
                self.assertFalse(result["issues"][0]["resolved"])

    def test_same_attempt_completion_resolves_step_truncation(self):
        events = [event("step", step=1, finish_reason="length", attempt_id="a", run_id="r"),
                  event("task_end", status="completed", attempt_id="a", run_id="r")]
        result = diagnose([task(status="completed")], events, [report("passed", spec={})])
        self.assertEqual(result["execution_status"], "finished")
        self.assertTrue(result["issues"][0]["resolved"])

    def test_recovered_plan_does_not_hide_a_subsequent_execution_error(self):
        events = [event("plan_usage", finish_reason="length", attempt_id="a", run_id="r"),
                  event("plan", attempt_id="a", run_id="r"),
                  event("task_end", status="error", error="worker error", attempt_id="a", run_id="r")]
        result = diagnose([task(status="failed")], events, [report()])
        self.assertEqual(result["execution_status"], "failed")
        self.assertTrue(result["issues"][0]["resolved"])
        errors = [issue for issue in result["issues"] if issue["severity"] == "error"]
        self.assertEqual([issue["code"] for issue in errors], ["execution_failed"])
        self.assertEqual(errors[0]["message"], "worker error")

    def test_service_exception_diagnostic_has_safe_frame_locations_without_secret_message(self):
        root = make_temp_dir("studio-diagnostic-error")
        service = StudioService(root, Settings(api_key="private-test-credential"))
        try:
            def fail(*_args):
                raise RuntimeError("private-test-credential https://private-endpoint.invalid/secret")

            with patch.object(service, "_execute", side_effect=fail):
                run = service.start({"request_id": str(uuid.uuid4()), "mode": "demo"})
                deadline = time.monotonic() + 3
                while True:
                    detail = service.detail(run["id"])
                    if detail["run"]["status"] == "failed":
                        break
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
            diagnostic = detail["diagnostics"]
            self.assertEqual(diagnostic["execution_status"], "failed")
            issue = next(i for i in diagnostic["issues"] if i["code"] == "studio_error")
            self.assertEqual(issue["error_code"], "RuntimeError")
            self.assertTrue(issue["frames"])
            for frame in issue["frames"]:
                self.assertNotIn("/", frame["file"])
                self.assertEqual(set(frame), {"file", "function", "line"})
            raw_trace = (root / run["id"] / "trace.jsonl").read_text()
            for secret in ("private-test-credential", "private-endpoint.invalid"):
                self.assertNotIn(secret, json.dumps(detail))
                self.assertNotIn(secret, raw_trace)
        finally:
            service.close()
            cleanup(root)


if __name__ == "__main__":
    unittest.main()
