"""Persistence, real role routing, publication and continuation in a Studio group."""
import hashlib
import shutil
import json
import time
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.research import Page
from miniagent.schemas import Usage
from miniagent.studio_conversations import validate_conversation
from miniagent.studio_preview import html_preview
from miniagent.studio_service import StudioError, StudioService
from tests.support import make_temp_dir, cleanup
from tests.test_research import public_pdf_fixture
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_service import finished, request


def roster(*roles):
    return {"kind": "group", "members": list(roles), "profiles": [
        {"id": role, "name": role, "prompt": "Independent responsibility of " + role}
        for role in roles], "recipient": roles[0], "intent": "edit"}


def assign(role, objective):
    return tool("delegate_task", assignee=role, objective=objective)


def write(path, content):
    return tool("write_file", path=path, content=content)


class StudioCrewTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("room-crew")
        self.service = StudioService(self.root / "runs", Settings(api_key="test-sentinel"))

    def tearDown(self):
        self.service.close()
        cleanup(self.root)

    def execute(self, script, conversation, mode="team", **kwargs):
        provider = RecordingProvider(script)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.service.start(request(mode, conversation=conversation, **kwargs))
            detail = finished(self.service, run["id"])
        return detail, provider

    def test_member_can_retry_its_original_browser_check_in_its_original_working_directory(self):
        original = ("from pathlib import Path\npage.goto(Path('index.html').resolve().as_uri())\n"
                    "assert page.locator('h1').inner_text() == 'Ready', 'page title mismatch'")
        path = "site/index.html"
        script = [tool("delegate_task", assignee="dev", objective="Deliver the ready status page.",
                       deliverable_paths=[path], remaining_roles=["qa"]),
                  write(path, "<!doctype html><html><body><h1>Broken</h1><p>Current service readiness.</p></body></html>"), tool("run_browser", code=original, cwd="site", timeout=12),
                  tool("rerun_browser_check", tool_call_id="missing-call"),
                  tool("rerun_browser_check", tool_call_id="call_3_0"),
                  tool("run_browser", code="assert page.title() == ''"),
                  tool("edit_file", path=path, old_text="Broken", new_text="Ready"),
                  *[tool("read_file", path=f"absent-{i}.txt") for i in range(7)],
                  tool("rerun_browser_check", tool_call_id="call_3_0"), {"text":"Original browser assertion now passes."},
                  tool("delegate_task", assignee="qa", objective="Independently inspect the delivered page."),
                  tool("rerun_browser_check", tool_call_id="call_3_0"),
                  tool("read_file", path=path), {"text":"Independently read the ready page."},
                  {"text":"Delivery complete."}]
        detail, provider = self.execute(script, roster("lead", "dev", "qa"),
                                       objective="Deliver a tested status page.", acceptance_files=[path], limits={"max_steps":90})
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertIn("rerun_browser_check", provider.requests[14][1])
        self.assertIn("edit_file", provider.requests[14][1])
        trace = [json.loads(line) for line in (self.root / "runs" / detail["run"]["id"] / "trace.jsonl").read_text().splitlines()]
        results = [e for e in trace if e["kind"] == "tool_result" and e["tool"] == "rerun_browser_check"]
        self.assertEqual([r["ok"] for r in results], [False, False, True, False])
        self.assertTrue(all("没有这条" in results[i]["preview"] for i in (0, 3)))
        self.assertEqual([(r["replayed_tool_call_id"], r["cwd"], r["exit_code"]) for r in results[1:3]],
                         [("call_3_0", "site", 1), ("call_3_0", "site", 0)])
        dev = reports[0]
        self.assertTrue(dev["published"])
        self.assertEqual(dev["evidence"]["observed_failure"], [])
        self.assertEqual(dev["evidence"]["replayable_browser_call_ids"], [])
        self.assertIn("含浏览器断言的脚本退出 0", dev["evidence"]["check_signals"])
        checkpoints = {e["step"]:e["checkpoint"] for e in trace if e["kind"] == "member_work_checkpoint" and e["task_id"] == dev["task_id"]}
        self.assertTrue(checkpoints[3]["failed"] and checkpoints[5]["failed"])
        self.assertFalse(any(r.get("replayed_tool_call_id") for r in (results[0], results[3])))

    def test_host_tool_inventory_can_be_read_without_a_human_or_grant_of_permission(self):
        from miniagent.studio_crew import RoomCrew
        crew = object.__new__(RoomCrew)
        crew.lead, crew.profiles = "manager", {"manager": {}, "builder": {}}
        question = "请确认本轮成员是否具备运行 Python 的能力？"
        message = crew.human_request_gate({"question": question, "decision_kind": "missing_user_data"})
        self.assertIn("run_python", message)
        self.assertTrue(crew.routing_denied)
        self.assertIsNone(crew.human_request_gate({"question": question, "decision_kind": "external_permission"}))
        self.assertIsNone(crew.human_request_gate({"question": "您的部署环境是否安装 Python？",
                                                   "decision_kind": "business_requirement"}))

    def test_software_team_delegates_tests_and_reuses_booking_records_in_dashboard(self):
        members = roster("lead", "dev", "qa", "release")
        booking_module = (
            "def book(rows, room, slot):\n"
            "    if any(r == [room, slot, 'active'] for r in rows):\n"
            "        return False\n"
            "    rows.append([room, slot, 'active'])\n"
            "    return True\n"
            "def cancel(rows, index):\n"
            "    rows[index][2] = 'cancelled'\n"
        )
        qa = ("import json\nexec(open('booking.py').read())\nrows = []\n"
              "assert book(rows, 'A', '10:00') and not book(rows, 'A', '10:00')\n"
              "cancel(rows, 0)\nassert book(rows, 'A', '10:00')\n"
              "with open('bookings.json', 'w') as f: json.dump(rows, f)\n"
              "print('conflict blocked and slot available after cancellation')")
        script = [assign("dev", "implement booking"), write("booking.py", booking_module),
                  {"text": "Saved booking.py."}, assign("qa", "check conflict and cancellation"),
                  tool("run_python", code=qa), {"text": "Verified the slot was freed; bookings.json is attached."},
                  assign("release", "package the runnable deliverable"),
                  tool("read_file", path="bookings.json"),
                  write("README.md", "Read booking.py for booking and cancellation operations."),
                  {"text": "Release notes ready."}, {"text": "Released."}]
        first, calls = self.execute(script, members, objective="Build booking", acceptance_files=["booking.py", "README.md"],
                                    child_checks={"qa": {"checks": [{"kind": "json_equals", "path": "bookings.json",
                                                                    "pointer": "/0/2", "expected": "cancelled"}]}})
        self.assertEqual(first["run"]["status"], "completed", first["diagnostics"])
        self.assertEqual({row["assignee_profile_id"] for row in first["tasks"]}, set(members["members"]))
        self.assertEqual([e["role"] for e in first["room_events"] if e["kind"] == "member_report"],
                         ["dev", "qa", "release"])
        self.assertTrue(all(e["published"] for e in first["room_events"] if e["kind"] == "member_report"))
        self.assertIn("Independent responsibility of qa", str(calls.requests[4][0]))
        self.assertIn("Saved booking.py", str(calls.requests[4][0]))
        self.assertIn("conflict blocked and slot available", str(calls.requests[5][0]))
        self.assertTrue(all(not a["path"].startswith("assignments/") for a in first["artifacts"]))
        run_id = first["run"]["id"]
        self.service.close()
        self.service = StudioService(self.root / "runs", Settings(api_key="test-sentinel"))
        compute = ("import json\nrows = json.load(open('bookings.json'))\n"
                   "active = sum(r[2] == 'active' for r in rows)\n"
                   "cancelled = len(rows) - active\n"
                   "open('dashboard.html','w').write(f'<p>active={active}, cancelled={cancelled}</p>')")
        second, dashboard_provider = self.execute([assign("dev", "compute metrics from prior records"),
                                  tool("run_python", code=compute), {"text": "Dashboard updated."},
                                  {"text": "Dashboard shipped."}],
                                 {"parent_run_id": run_id, "recipient": "lead", "intent": "edit"},
                                 objective="Build dashboard", acceptance_checks=[{"kind":"contains","path":"dashboard.html",
                                                                                 "text":"active=1, cancelled=1"}])
        self.assertEqual(second["run"]["status"], "completed", second["diagnostics"])
        self.assertEqual(second["conversation"]["id"], run_id)
        first_prompt = dashboard_provider.requests[0][0][-1]["content"]
        inventory = json.loads(first_prompt.split("未发布草稿不影响该清单）：",1)[1])
        self.assertEqual(inventory["base_run_id"], run_id)
        self.assertIn({"path":"bookings.json","bytes":len(json.dumps([["A","10:00","cancelled"],["A","10:00","active"]]))},
                      inventory["files"])
        self.assertEqual(second["conversation"]["profiles"], members["profiles"])
        project = self.root / "runs" / second["run"]["id"] / "agents" / "lead"
        self.assertEqual(json.loads((project / "bookings.json").read_text()),
                         [["A", "10:00", "cancelled"], ["A", "10:00", "active"]])

    def test_group_final_requires_member_and_enforces_check(self):
        group = roster("fund_manager", "research", "risk", "marketing")
        bad, provider = self.execute([assign("outsider", "do work"), {"text": "complete"},
                                      {"text": "no member used"}], group, objective="Plan portfolio")
        self.assertIn("请明确指定本组", str(provider.requests[1][0]))
        self.assertEqual(bad["run"]["status"], "failed")
        self.assertEqual(len(bad["tasks"]), 1)
        self.assertFalse(bad["run"]["error_code"])
        self.assertTrue(any(e["kind"] == "task_end" for e in bad["events"]))
        no_report, _ = self.execute([assign("risk", "audit"), write("partial.md", "incomplete"),
                                      {"text": "done"}, {"text":"still incomplete"},
                                      {"text": "finished"}, {"text": "finished"}], group,
                                     objective="Review risk", child_checks={"risk":{"files":["risk.md"]}})
        self.assertEqual(no_report["run"]["status"], "failed")
        self.assertFalse(no_report["room_events"][-1]["published"])
        self.assertEqual(no_report["room_events"][-1]["artifacts"], [])
        run_dir = self.root / "runs" / no_report["run"]["id"]
        draft = next((run_dir / "assignments").glob("*/agents/risk/partial.md"))
        with self.assertRaises(StudioError):
            self.service.download_artifact(run_dir.name, draft.relative_to(run_dir).as_posix())
        self.assertFalse((run_dir / "handoffs").exists())
        self.assertFalse((self.root / "runs" / no_report["run"]["id"] / "agents/fund_manager/partial.md").exists())

    def test_member_receipts_pin_each_version_after_same_path_changes(self):
        first_html = '<link rel="stylesheet" href="page.css"><h1>first</h1>'
        second_html = first_html.replace("first", "second")
        script = [tool("delegate_task", assignee="dev", objective="create initial page", remaining_roles=["qa"]),
                  write("page.css", "h1{color:red}"), write("index.html", first_html), {"text":"v1 delivered"},
                  tool("delegate_task", assignee="qa", objective="revise the heading", remaining_roles=[]),
                  write("index.html", second_html), {"text":"v2 delivered"}, {"text":"done"}]
        detail, _ = self.execute(script, roster("lead","dev","qa"), objective="Build and revise the page",
                                 acceptance_files=["index.html"])
        self.assertEqual(detail["run"]["status"], "completed", detail["run"])
        run_id = detail["run"]["id"]
        reports = [e for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual([r["role"] for r in reports], ["dev", "qa"])
        paths = []
        for report, content in zip(reports, [first_html, second_html]):
            link = next(f for f in report["artifacts"] if f["name"]=="index.html")
            self.assertEqual(link["project_path"], "index.html")
            self.assertEqual(link["size"], len(content.encode()))
            self.assertEqual(link["sha256"], hashlib.sha256(content.encode()).hexdigest())
            self.assertEqual(self.service.download_artifact(run_id, link["path"]), content.encode())
            self.assertEqual(self.service.artifact(run_id, link["path"])["content"], content)
            # The second version inherits its stylesheet from the first.
            self.assertIn(b"iframe", html_preview(self.service, run_id, link["path"]))
            paths.append(link["path"])
        self.assertNotEqual(paths[0], paths[1])
        self.assertEqual(self.service.artifact(run_id,"agents/lead/index.html")["content"], second_html)
        self.assertEqual([f["name"] for f in reports[1]["artifacts"]], ["index.html"])
        snapshot = paths[0].split("/")[1]
        stored = self.root / "runs" / run_id / "handoffs" / snapshot / "project/index.html"
        stored.write_text("changed after publication")
        with self.assertRaises(StudioError) as conflict:
            self.service.download_artifact(run_id, paths[0])
        self.assertEqual(conflict.exception.code, "handoff_conflict")

    def test_same_newsroom_new_topic_can_explicitly_start_without_old_files(self):
        group = roster("chief_editor", "reporter", "fact_checker", "editor")
        first, _ = self.execute([write("news.md", "Old newsroom story on campus event."), {"text": "Old story published."}],
                                group, mode="single", objective="Campus story")
        second, calls = self.execute([tool("list_dir"), {"text": "New subject."}],
                                     {"parent_run_id":first["run"]["id"], "recipient":"fact_checker",
                                      "intent":"edit", "inherit_files":False}, mode="single", objective="Investigate AI detectors")
        self.assertEqual(first["conversation"]["id"], second["conversation"]["id"])
        self.assertNotIn("news.md", str(calls.requests[1][0][-1]))
        self.assertIn("Old story published.", str(calls.requests[0][0]))
        self.assertFalse((self.root / "runs" / second["run"]["id"] / "agents/fact_checker/news.md").exists())

    def test_explicit_recovery_starts_from_failed_partial_file(self):
        group = roster("lead","dev")
        first, _ = self.execute([write("project.md", "working version before failure"), {"text":"ok"}],
                                group, mode="single", objective="initial")
        bad, _ = self.execute([write("project.md", "partial work during failed attempt")],
                              {"parent_run_id":first["run"]["id"], "recipient":"dev", "intent":"edit"},
                              mode="single", objective="change", limits={"max_steps":1})
        self.assertEqual(bad["run"]["status"], "failed")
        self.assertEqual(bad["conversation"]["artifact_run_id"], first["run"]["id"])
        recovered, _ = self.execute([tool("edit_file", path="project.md", old_text="partial work",
                                           new_text="finished work"), {"text":"recovered"}],
                                    {"parent_run_id":bad["run"]["id"], "recipient":"lead", "intent":"edit",
                                     "recover_failed":True}, mode="single", objective="complete failed version",
                                    acceptance_checks=[{"kind":"contains","path":"project.md","text":"finished work during failed"}])
        self.assertEqual(recovered["run"]["status"], "completed")
        with self.assertRaises(StudioError):
            self.service.start(request("single", objective="invalid", conversation={
                "parent_run_id": recovered["run"]["id"], "recipient":"dev", "intent":"edit", "recover_failed":True}))

    def test_roles_and_continuation_constraints(self):
        for members in [roster("lead", "../bad"), roster("lead","LEAD"),
                        {**roster("lead","dev"), "profiles":[]},
                        {"parent_run_id":"x","recipient":"lead","intent":"edit","recover_failed":True,"inherit_files":False},
                        {"parent_run_id":"x","recipient":"lead","intent":"question","profiles":[]}]:
            with self.subTest(members=members), self.assertRaises(ValueError):
                validate_conversation(members)

    def test_newsroom_verifies_retrieved_evidence_across_two_distinct_assignments(self):
        group = roster("chief_editor", "reporter", "fact_checker", "editor")
        cases = [("news", "HKUST campus AI", "campus", "A student AI workshop was held on campus."),
                 ("papers", "AI writing detection", "detectors", "False positives occurred; attribution remains uncertain.")]
        previous = None
        for collection, query, stem, excerpt in cases:
            url = "https://example.org/" + stem
            search = ({"articles": [{"title": query, "url": url}]} if collection == "news" else
                      {"message":{"items":[{"title":[query], "URL":url}]}})
            with (patch("miniagent.research._service",return_value=Page(url, "application/json", json.dumps(search).encode(), False)),
                  patch("miniagent.research.fetch", return_value=Page(url, "text/plain", excerpt.encode(), False))):
                # The reviewer derives the cited excerpt from the reporter's
                # actual snapshot, including the retrieved URL and bytes.
                review = ("import json, pathlib\nentries = [json.loads(p.read_text()) for p in pathlib.Path('sources').glob('*.json')]\n"
                          "record = next(r for r in entries if r['kind'] == 'fetched_source')\n"
                          "pathlib.Path('checked.md').write_text(record['url'] + '\\n' + record['text'])")
                script = [assign("reporter", "Find primary source for " + query),
                          tool("search_sources", query=query, collection=collection),
                          tool("http_get", url=url), {"text":"Retrieved source for review."},
                          assign("fact_checker", "Independently check the fetched excerpt"), tool("run_python", code=review),
                          {"text":"Checked the excerpt in the source archive."},
                          assign("editor", "prepare the copy"), tool("read_file", path="checked.md"),
                          write(stem+".md", query+"\n"+excerpt+"\n"+url), {"text":"Public copy ready."},
                          {"text":"Newsroom delivery is ready."}]
                conversation = (group if not previous else {"parent_run_id": previous["run"]["id"],
                                "recipient":"chief_editor","intent":"edit","inherit_files":False})
                current, provider = self.execute(script, conversation, objective=query, acceptance_checks=[
                    {"kind":"contains","path":stem+".md","text":excerpt}],
                    child_checks={"fact_checker":{"checks":[{"kind":"contains","path":"checked.md","text":url}]}})
            self.assertEqual(current["run"]["status"], "completed", current["diagnostics"])
            self.assertIn(excerpt, str(provider.requests[9][0][-1]))
            if previous:
                self.assertEqual(current["conversation"]["id"], previous["conversation"]["id"])
                self.assertIn("HKUST campus AI", str(provider.requests[0][0]))
                self.assertNotIn("campus.md", {a["name"] for a in current["artifacts"]})
            previous = current

    def test_no_cumulative_cap_delegates_and_counts_usage_past_a_million(self):
        # Every individual prompt fits the model window. Only the sum exceeds 1M.
        class Metered(RecordingProvider):
            def chat(self, messages, tools=None, temperature=0):
                result = super().chat(messages, tools, temperature)
                return result.model_copy(update={"usage": Usage(prompt_tokens=310_000, completion_tokens=100)})

        provider = Metered([tool("delegate_task", assignee="dev", objective="write the file",
                                 remaining_roles=[], deliverable_paths=["work.txt"]),
                            write("work.txt", "Completed: existing project files verified."),
                            {"text": "I wrote work.txt."}, {"text": "Delivered."}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.service.start(request("team", conversation=roster("lead", "dev"),
                                             objective="write a file", acceptance_files=["work.txt"],
                                             child_checks={"dev": {"files": ["work.txt"]}}, limits={"max_total_tokens": 0}))
            detail = finished(self.service, run["id"])
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        calls = [e for e in detail["events"] if e["kind"] == "step_start"]
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(e["tokens_remaining"] is None for e in calls))
        self.assertEqual([e["tokens_used"] for e in calls], [0, 0, 310_100, 930_300])
        steps = [e for e in detail["events"] if e["kind"] == "step"]
        self.assertEqual(sum(e["prompt_tokens"] + e["completion_tokens"] for e in steps), 1_240_400)
        [lease] = [e for e in detail["room_events"] if e["kind"] == "assignment"]
        self.assertIsNone(lease["allocation"]["tokens_left"])
        self.assertEqual(lease["reserved"]["tokens_left"], 0)
        self.assertEqual(calls[-1]["offered_tools"], [])

    def test_shared_tokens_debit_lead_before_child_and_charge_child_to_lead(self):
        class Metered(RecordingProvider):
            def chat(self, messages, tools=None, temperature=0):
                result = super().chat(messages, tools, temperature)
                return result.model_copy(update={"usage":Usage(prompt_tokens=500,completion_tokens=100)})
        provider = Metered([assign("dev","design it"), {"text":"Design provided."}, {"text":"Summary."}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.service.start(request("team", conversation=roster("lead","dev"),
                                     objective="Budgeted room", limits={"max_total_tokens":20000,"max_steps":6}))
            detail = finished(self.service,run["id"])
        calls = [e for e in detail["events"] if e["kind"] == "step_start"]
        [lease] = [e for e in detail["room_events"] if e["kind"] == "assignment"]
        child_tokens = lease["allocation"]["tokens_left"]
        self.assertEqual(child_tokens + lease["reserved"]["tokens_left"], 19400)
        self.assertGreater(lease["reserved"]["tokens_left"], 1000)
        self.assertEqual([e["tokens_remaining"] for e in calls], [20000, child_tokens, 18800])
        self.assertEqual(calls[-1]["offered_tools"], [])
        self.assertEqual([e["level"] for e in calls], ["goal", "subtask", "goal"])
        self.assertEqual(detail["run"]["status"], "unverified")
        limited, p = self.execute([assign("dev","design"), {"text":"no member budget"}], roster("lead","dev"),
                                  objective="no spare steps", limits={"max_steps":2})
        self.assertEqual(limited["run"]["status"], "failed")
        self.assertEqual(len(limited["tasks"]), 1)
        self.assertIn("已无委派预算", str(p.requests[1][0]))

    def test_elected_plan_protects_other_members_and_closes_from_public_receipts(self):
        group = roster("lead", "writer", "reviewer", "unused")
        first = tool("delegate_task", assignee="writer", objective="Prepare the text",
                     remaining_roles=["reviewer"])
        script = [first, write("work.txt", "Draft checked with the team."),
                  {"text": "Published work.txt."}, {"text": "Premature completion"},
                  assign("reviewer", "Check the published draft"), tool("read_file", path="work.txt"),
                  {"text": "The text covers the request."}, {"text": "Ready."}]
        detail, provider = self.execute(script, group, objective="Prepare a reviewed draft",
                                        limits={"max_steps": 100, "max_total_tokens": 1_000_000}, acceptance_files=["work.txt"])
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertIn("reviewer", str(provider.requests[4][0]))
        self.assertEqual(provider.requests[-1][1], [])
        goals = [e for e in detail["events"] if e["kind"] == "step_start" and e["level"] == "goal"]
        self.assertIn("read_lines",goals[0]["offered_tools"])
        for event in goals[1:]:
            self.assertLessEqual(set(event["offered_tools"]), {"abort_task", "delegate_task", "ask_human"})
        assignments = [e for e in detail["room_events"] if e["kind"] == "assignment"]
        self.assertEqual([e["recipient"] for e in assignments], ["writer", "reviewer"])
        first, last = assignments
        self.assertEqual(first["planned"], ["reviewer", "writer"])
        for dimension in ["steps_left", "tokens_left", "actions_left", "seconds_left"]:
            self.assertGreater(first["reserved"][dimension], 0)
            self.assertLess(first["allocation"][dimension], last["allocation"][dimension])
        reports = [e for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual(reports[0]["handoff"]["changed_files"][0]["path"], "work.txt")
        self.assertEqual(len(reports[0]["handoff"]["changed_files"][0]["sha256"]), 64)
        self.assertEqual(reports[1]["handoff"]["changed_files"], [])
        self.assertIn("work.txt", reports[1]["handoff"]["evidence"]["inspected"])

    def test_member_and_lead_each_correct_unexecuted_tool_text_within_six_room_steps(self):
        pseudo = '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="run_shell">touch ghost.txt'
        script = [tool("delegate_task", assignee="dev", objective="Write result", remaining_roles=[]),
                  write("receipt.md", "Deliverable verified independently."),
                  {"text": pseudo}, {"text": "Wrote receipt.md."},
                  {"text": pseudo}, {"text": "Delivered receipt.md."}]
        detail, provider = self.execute(script, roster("lead", "dev"), objective="Write a receipt",
                                        acceptance_files=["receipt.md"], limits={"max_steps": 6})
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertEqual(len(provider.requests), 6)
        self.assertEqual([names for _, names in provider.requests[2:]], [[], [], [], []])
        self.assertTrue(all("工具协议纠正" in str(provider.requests[i][0]) for i in (3, 5)))
        self.assertFalse(list(self.root.rglob("ghost.txt")))
        fixes = [e for e in detail["events"] if e["kind"] == "tool_protocol_correction"]
        self.assertEqual([(e["phase"], e["retry"]) for e in fixes], [("handoff", True), ("finalize", True)])

    def test_two_step_member_lease_keeps_its_work_turn_and_cannot_overrun_room(self):
        pseudo = "<｜tool▁calls▁begin｜> name=run_shell"
        script = [tool("delegate_task", assignee="dev", objective="Write result", remaining_roles=[]),
                  write("receipt.md", "A real but unaccepted draft."),
                  {"text": pseudo}, {"text": "[BLOCKED] No published receipt."}]
        detail, provider = self.execute(script, roster("lead", "dev"), objective="Write a receipt",
                                        acceptance_files=["receipt.md"], limits={"max_steps": 4})
        self.assertEqual(detail["run"]["status"], "failed")
        self.assertEqual(len(provider.requests), 4)
        self.assertIn("write_file", provider.requests[1][1])
        self.assertEqual(provider.requests[2][1], [])
        reports = [e for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertFalse(reports[0]["published"])
        self.assertEqual(reports[0]["status"], "limit_exceeded")

    def test_addressed_member_retains_tools_and_sees_missing_file_after_repeating_a_read(self):
        path = "notes/research.md"
        script = [tool("list_dir", path=".")] * 9 + [
            write(path, "Documented the requested finding."), {"text":"Research complete."}]
        result, provider = self.execute(script, roster("lead", "writer", "reviewer"),
                                        objective="@writer Write up the finding.", acceptance_files=[path])
        self.assertEqual(result["run"]["status"], "completed", result["diagnostics"])
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][0]["assignee_profile_id"], "writer")
        request, tools = provider.requests[9]
        self.assertEqual(tools, provider.requests[0][1])
        for text in ("进度提醒", "list_dir .", path):
            self.assertIn(text, str(request[-1]["content"]))

    def test_stalled_member_can_write_declared_file_then_verify_before_handoff(self):
        path = "notes/research.md"
        content = "Checked content from the captured evidence. Remaining claims are explicitly unconfirmed."
        exploration = [tool("run_python", code="print('research material')")] * 10
        script = [tool("delegate_task", assignee="writer", objective="Write the verified note",
                       deliverable_paths=[path], remaining_roles=[]),
                  *exploration, tool("list_dir", path="."), write(path, content),
                  tool("run_python", code=f"from pathlib import Path\nassert Path({path!r}).read_text() == {content!r}"),
                  {"text":"Ready"}, {"text":"Delivered"}]
        detail, provider = self.execute(script, roster("lead","writer","observer"), objective="Create a research note",
                                       acceptance_files=[path], limits={"max_seconds":120,"max_total_tokens":0,"max_steps":0})
        self.assertEqual(detail["run"]["status"], "completed", detail["report"])
        calls = [row for row in detail["events"] if row["kind"] == "step_start" and row["level"] == "subtask"]
        self.assertTrue(all(row["phase"] == "work" for row in calls))
        self.assertTrue(all(row["offered_tools"] == calls[0]["offered_tools"] for row in calls))
        for name in ("write_file", "read_source", "edit_file", "run_python", "list_dir"):
            self.assertIn(name, calls[-1]["offered_tools"])
        after_repeats = str(provider.requests[11][0])
        for text in ("进度提醒", "run_python", path, "research material"):
            self.assertIn(text, after_repeats)
        report, = [row["handoff"] for row in detail["room_events"] if row["kind"] == "member_report"]
        self.assertTrue(report["published"])
        self.assertEqual([f["path"] for f in report["changed_files"]], [path])

    def test_writer_receives_edit_trend_and_can_rewrite_the_whole_draft(self):
        path = "guide.md"
        edit = ("from pathlib import Path\np = Path('guide.md')\n"
                "body = p.read_text().replace('Draft', 'Done', 1)\np.write_text(body)\n"
                "print('remaining:', body.count('Draft'))")
        substantial_revision = "A revised guide covering the requested material."
        script = [tool("delegate_task", assignee="writer", objective="Revise the draft until it is ready",
                       deliverable_paths=[path], remaining_roles=[]),
                  write(path, "Draft paragraph.\n" * 100),
                  *[tool("run_python", code=edit) for _ in range(5)],
                  write(path, substantial_revision), {"text":"Ready"}, {"text":"Delivered"}]
        detail, provider = self.execute(script, roster("lead", "writer", "observer"),
                                        objective="Prepare the guide", acceptance_files=[path])
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        request, tools = provider.requests[7]
        self.assertEqual(tools, provider.requests[1][1])
        for fact in (path, "5 次局部小改", "remaining: 99", "remaining: 95"):
            self.assertIn(fact, request[-1]["content"])
        self.assertNotIn("局部小改", provider.requests[8][0][-1]["content"])
        report, = [row["handoff"] for row in detail["room_events"] if row["kind"] == "member_report"]
        self.assertTrue(report["published"])
        self.assertEqual(report["changed_files"][0]["sha256"], hashlib.sha256(substantial_revision.encode()).hexdigest())

    def test_repeated_green_suite_allows_member_to_decide_on_handoff(self):
        code = ("import subprocess, sys\n"
                "p = subprocess.run([sys.executable, '-m', 'unittest', 'test_delivery'], capture_output=True, text=True)\n"
                "print(p.stderr)\nraise SystemExit(p.returncode)")
        suite = "import unittest\nclass Check(unittest.TestCase):\n    def test_one(self): self.assertEqual(1+1, 2)"
        script = [tool("delegate_task", assignee="dev", objective="Implement and check", remaining_roles=[]),
                  write("test_delivery.py", suite), tool("run_python", code=code), tool("run_python", code=code),
                  {"text": "Two runs of the single test passed."}, {"text": "Delivered the verified suite."}]
        detail, provider = self.execute(script, roster("lead", "dev", "unrelated"), objective="Build a checked module",
                                        acceptance_files=["test_delivery.py"])
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        calls = [e for e in detail["events"] if e["kind"] == "step_start"]
        self.assertEqual([e["phase"] for e in calls], ["coordinate", "work", "work", "work", "work", "finalize"])
        self.assertIn("run_python", provider.requests[-2][1])
        self.assertIn("进度提醒", str(provider.requests[-2][0]))
        self.assertEqual(provider.requests[-1][1], [])
        [report] = [e for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertTrue(report["handoff"]["evidence"]["check_signals"])
        self.assertEqual(report["handoff"]["evidence"]["stop_reason"], "")

    @unittest.skipUnless(shutil.which("pdftotext"), "Poppler is optional")
    def test_researcher_recovers_own_pdf_without_losing_formal_source(self):
        page = public_pdf_fixture()
        prepare = ("from pathlib import Path\nPath('downloads').mkdir()\n"
                   f"Path('downloads/uce_list.pdf').write_bytes(bytes.fromhex('{page.body.hex()}'))\n"
                   "Path('Library/Caches/tool').mkdir(parents=True)\n"
                   "Path('Library/Caches/tool/response.bin').write_bytes(b'cache')")
        recover = ("import tempfile\nfrom pathlib import Path\nfrom shutil import move\n"
                   "with tempfile.TemporaryDirectory() as folder:\n"
                   "    path = Path(folder)/'uce_list.pdf'\n"
                   "    move('downloads/uce_list.pdf', path)\n"
                   "    move('Library/Caches/tool/response.bin', Path(folder)/'response.bin')\n"
                   "    assert path.read_bytes().startswith(b'%PDF-')")
        script = [tool("delegate_task",assignee="writer",objective="Prepare a sourced catalogue report.",
                       remaining_roles=[],deliverable_paths=["catalogue.md"]),
                  tool("run_python",code=prepare),tool("http_get",url=page.url,pdf_page=2),
                  write("catalogue.md","The official PDF lists the Course catalogue on page two."),
                  {"text":"My sourced report is ready."},tool("run_python",code=recover),
                  {"text":"The sourced report is ready."},{"text":"Delivered."}]
        with patch("miniagent.research.fetch",return_value=page):
            detail,provider = self.execute(script,roster("lead","writer"),objective="Deliver the sourced report.",
                                          acceptance_files=["catalogue.md"])
        self.assertEqual(detail["run"]["status"],"completed",detail["diagnostics"])
        first_member_prompt = str(provider.requests[1][0])
        self.assertIn("tempfile.TemporaryDirectory",first_member_prompt)
        self.assertIn("sources/*.json",first_member_prompt)
        self.assertIn("downloads/uce_list.pdf",str(provider.requests[5][0]))
        self.assertIn("Library/Caches/tool/response.bin",str(provider.requests[5][0]))
        pub = self.root/"runs"/detail["run"]["id"]/"agents"/"lead"
        self.assertTrue((pub/"catalogue.md").is_file())
        [source] = (pub/"sources").glob("*.json")
        import json
        evidence = json.loads(source.read_text())
        self.assertEqual((evidence["text"],evidence["pdf_page"]),("Course catalogue",2))
        self.assertEqual(evidence["observed_bytes_sha256"],hashlib.sha256(page.body).hexdigest())
        self.assertFalse(list(pub.rglob("*.pdf")))

    def test_requested_unsupported_original_stays_in_private_tree(self):
        script = [tool("delegate_task",assignee="writer",objective="Deliver the original signed PDF.",remaining_roles=[]),
                  tool("run_python",code="from pathlib import Path\nPath('signed-original.pdf').write_bytes(b'%PDF-1.4')"),
                  {"text":"The original is ready."},{"text":"[BLOCKED] The required original PDF is unsupported."},
                  {"text":"[BLOCKED] Original PDF delivery remains open."}]
        detail,provider = self.execute(script,roster("lead","writer"),objective="Deliver the original signed PDF.")
        self.assertEqual(detail["run"]["status"],"failed")
        feedback = str(provider.requests[3][0])
        self.assertIn("signed-original.pdf",feedback)
        self.assertIn("保留并报告格式限制",feedback)
        run = self.root/"runs"/detail["run"]["id"]
        [original] = run.glob("assignments/*/agents/writer/signed-original.pdf")
        self.assertEqual(original.read_bytes(),b"%PDF-1.4")
        self.assertFalse((run/"agents"/"lead"/"signed-original.pdf").exists())
        self.assertFalse(any(e.get("published") for e in detail["room_events"] if e["kind"]=="member_report"))

    def test_blocked_or_rejected_member_publication_prevents_success_after_good_draft(self):
        for last, reason in [([{"text": "[BLOCKED] missing proof"}], "missing proof"),
                             ([write("bad.xyz", "unsupported format"), {"text": "Ready to publish."},
                               {"text": "[BLOCKED] Cannot publish the required bad.xyz file."}], "bad.xyz")]:
            with self.subTest(reason=reason):
                script = [tool("delegate_task", assignee="writer", objective="Create a draft", remaining_roles=["check"]),
                          write("draft.md", "Good enough for review."), {"text": "Published draft."},
                          assign("check", "Inspect delivery"), *last,
                          {"text": "Everything passed."}, {"text": "[BLOCKED] Independent handoff remains unavailable."}]
                detail, provider = self.execute(script, roster("lead", "writer", "check"), objective="Deliver a checked draft",
                                         acceptance_files=["draft.md"])
                self.assertEqual(detail["run"]["status"], "failed", detail["diagnostics"])
                reports = [e for e in detail["room_events"] if e["kind"] == "member_report"]
                self.assertTrue(reports[0]["published"])
                self.assertEqual(reports[1]["disposition"], "blocked")
                self.assertIn(reason, reports[1]["handoff"]["remaining_issue"])
                if reason == "bad.xyz":
                    self.assertIn("项目快照发布失败", str(provider.requests))
                self.assertFalse((self.root / "runs" / detail["run"]["id"] / "agents/lead/bad.xyz").exists())

    def test_blocked_draft_is_recoverable_only_by_its_owner_on_the_same_public_revision(self):
        first = tool("delegate_task", assignee="writer", objective="Prepare both files",
                     deliverable_paths=["draft.md", "guide.md"], remaining_roles=["review"])
        for published_in_between in [False, True]:
            with self.subTest(changed_public_version=published_in_between):
                script = [first, write("draft.md", "PRIVATE DRAFT FROM FIRST ATTEMPT"), {"text": "[BLOCKED] missing guide"}]
                if published_in_between:
                    script.extend([assign("review", "Publish project constraints"), tool("list_dir"),
                                   write("constraints.md", "New public constraints."), {"text": "Constraints ready."}])
                script.extend([tool("delegate_task", assignee="writer", objective="Finish the missing guide",
                                    deliverable_paths=["draft.md", "guide.md"]), tool("list_dir")])
                if published_in_between:
                    script.append(write("draft.md", "Fresh draft on the new revision."))
                script.extend([write("guide.md", "The guide is now ready."), {"text": "Published both files."}])
                if not published_in_between:
                    script.extend([assign("review", "Verify the two files"), tool("read_file", path="guide.md"),
                                   {"text": "Reviewed the guide."}])
                script.append({"text": "Delivered the reviewed draft."})
                detail, provider = self.execute(script, roster("lead", "writer", "review"), objective="Write and review",
                                                acceptance_files=["draft.md", "guide.md"])
                self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
                rows = [e for e in detail["room_events"] if e["kind"] == "assignment" and e["recipient"] == "writer"]
                self.assertEqual(rows[1]["resumed_private_draft"], not published_in_between)
                reports = [e for e in detail["room_events"] if e["kind"] == "member_report" and e["role"] == "writer"]
                self.assertFalse(reports[0]["published"])
                self.assertEqual({c["path"] for c in reports[1]["handoff"]["changed_files"]}, {"draft.md", "guide.md"})
                self.assertIn("guide.md", reports[0]["handoff"]["evidence"]["missing_required_files"])
                if published_in_between:
                    self.assertNotIn("draft.md", str(provider.requests[5][0][-1]))

    def test_rephrased_stalled_retries_preserve_peer_lease_and_allow_explicit_repair(self):
        for role_bound in [False, True]:
            with self.subTest(role_bound=role_bound):
                script = [tool("delegate_task", assignee="writer", objective="Build source and guide",
                               remaining_roles=["review"], deliverable_paths=["app.py", "guide.md"]),
                          write("app.py", "print('hello world')"), {"text":"[BLOCKED] guide still needed"},
                          assign("writer", "Finish the guide"), {"text":"[BLOCKED] could not finish"},
                          assign("writer", "Try once more and complete missing documentation"),
                          tool("delegate_task", assignee="review", objective="Repair the missing guide",
                               recover_from="writer", deliverable_paths=["guide.md"]),
                          write("guide.md", "Run python app.py for a greeting."), {"text":"Missing guide supplied."},
                          {"text":"Ready."}, {"text":"Ready after review."}]
                kwargs = {"child_checks":{"writer":{"files":["guide.md"]}}} if role_bound else {}
                detail,provider = self.execute(script, roster("lead", "writer", "review"), objective="Deliver app and guide",
                                               limits={"max_steps": 100, "max_total_tokens": 1_000_000}, acceptance_files=["guide.md", "app.py"], **kwargs)
                self.assertEqual(detail["run"]["status"], "failed" if role_bound else "completed",detail["diagnostics"])
                assignments = [e for e in detail["room_events"] if e["kind"]=="assignment"]
                self.assertEqual([e["recipient"] for e in assignments], ["writer", "writer", "review"])
                floor = assignments[0]["protected_peer_shares"]["review"]
                self.assertEqual(floor, assignments[1]["protected_peer_shares"]["review"])
                for dim in ("steps_left","tokens_left","actions_left"):
                    self.assertGreaterEqual(assignments[-1]["allocation"][dim],floor[dim])
                self.assertEqual(assignments[-1]["recover_from"], "writer")
                self.assertIn("同一公开版本", str(provider.requests[6][0]))
                self.assertTrue({"write_file","edit_file","run_python","read_file"} <= set(provider.requests[4][1]))
                reports = [e for e in detail["room_events"] if e["kind"]=="member_report"]
                self.assertEqual([e["published"] for e in reports], [False,False,True])
                self.assertEqual({f["path"] for f in reports[-1]["handoff"]["changed_files"]}, {"app.py","guide.md"})

    def test_recovery_counts_changed_go_typescript_and_json_config_while_a_path_is_pending(self):
        same_task = tool("delegate_task", assignee="dev", objective="Prepare app and guide",
                         remaining_roles=[], deliverable_paths=["guide.md"])
        script = [same_task,
                  write("main.go", "package main\n"), {"text":"[BLOCKED] guide pending"},
                  same_task, write("main.go","package main\nfunc main() {}\n"),
                  {"text":"[BLOCKED] guide pending"},
                  same_task, write("view.ts","export const view = 1;\n"),
                  {"text":"[BLOCKED] guide pending"},
                  same_task, write("tsconfig.json",'{"compilerOptions":{"target":"ES2022"}}'),
                  {"text":"[BLOCKED] guide pending"},
                  same_task, write("guide.md","Run go and build the TypeScript client."),
                  {"text":"Ready"}, {"text":"Delivered"}]
        detail, _ = self.execute(script, roster("lead","dev"), objective="Finish the app and guide",
                                 acceptance_files=["main.go","view.ts","guide.md","tsconfig.json"],
                                 limits={"max_steps":45,"max_total_tokens":500000,"max_seconds":90})
        self.assertEqual(detail["run"]["status"], "completed",detail["diagnostics"])
        reports = [e for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual([e["published"] for e in reports],[False,False,False,False,True])
        self.assertEqual({f["path"] for f in reports[-1]["handoff"]["changed_files"]},
                         {"main.go","view.ts","tsconfig.json","guide.md"})

    def test_runtime_routes_exhausted_worker_to_first_elected_peer_with_real_failed_check(self):
        check = "from pathlib import Path\nassert Path('app.py').read_text() == 'print(1)\\n'\nprint('value correct')"
        script = [tool("delegate_task", assignee="dev", objective="Deliver the application",
                       remaining_roles=["verifier","release"], deliverable_paths=["app.py"]),
                  write("app.py", "print(2)\n"), tool("run_python",code=check),
                  tool("read_file",path="app.py"), tool("read_file",path="app.py"),
                  {"text":"[BLOCKED] The actual value assertion failed."},
                  assign("dev","Fix the actual value"), assign("dev","Please retry the fix"),
                  write("app.py","print(1)\n"), tool("run_python",code=check),
                  {"text":"The original assertion passed on the repaired application."},
                  assign("release","Package the verified application"), tool("read_file",path="app.py"),
                  {"text":"Reusable source available."}, {"text":"Application delivered."}]
        detail,_ = self.execute(script, roster("lead", "dev", "release", "verifier"), objective="Build checked app",
                                acceptance_checks=[{"kind":"contains","path":"app.py","text":"print(1)"}],
                                limits={"max_steps":20})
        self.assertEqual(detail["run"]["status"],"completed",detail["diagnostics"])
        events = detail["room_events"]
        assignments = [e for e in events if e["kind"]=="assignment"]
        self.assertEqual([(e["role"],e["recipient"]) for e in assignments],
                         [("lead","dev"),("studio","verifier"),("lead","release")])
        [reroute] = [e for e in events if e["kind"]=="runtime_reroute"]
        self.assertEqual([reroute["requested"],reroute["recipient"],reroute["recover_from"]],["dev","verifier","dev"])
        self.assertEqual([e["published"] for e in events if e["kind"]=="member_report"],[False,True,True])
        self.assertEqual(len(detail["tasks"]),4)

    def test_member_retains_real_source_and_verification_tools_while_writing_survey(self):
        group = roster("lead","writer")
        marker = "REAL-WORKSPACE-VALUE-4053"
        first,_ = self.execute([write("notes.md",marker+" evidence details "*10),{"text":"Initial file ready."}],
                               group,mode="single",objective="Create notes")
        reads = [tool("read_file",path="notes.md",offset=i,limit=60) for i in range(4)]
        detail,provider = self.execute([tool("delegate_task",assignee="writer",objective="Summarize notes",
                                             deliverable_paths=["survey.md"],remaining_roles=[]),
                                       *reads,write("survey.md",marker+" was observed."),{"text":"Written."},{"text":"Ready."}],
                                      {"parent_run_id":first["run"]["id"],"intent":"edit","recipient":"lead"},
                                      objective="Write a survey based on the actual notes",acceptance_files=["survey.md"])
        self.assertEqual(detail["run"]["status"],"completed",detail["diagnostics"])
        request,tools = provider.requests[5]
        self.assertTrue({"write_file","append_file","edit_file","read_file","run_python"} <= set(tools))
        self.assertIn(marker,str(request))

    def test_existing_feature_edit_and_real_verification_precede_written_check_report(self):
        group = roster("lead","dev")
        original = "# existing application behavior and compatibility notes\n" * 205 + "def occupied_minutes(rows):\n    return 0\n"
        parent,_ = self.execute([write("app.py",original),
                                 write("README.md","Run the application."),write("bookings.json","[60,45]"),
                                 {"text":"Source ready."}],group,mode="single",objective="Start application")
        reads = [tool("read_file",path="app.py",offset=n,limit=4800) for n in [0,4800,9600]]
        reads.extend([tool("read_file",path="README.md"),tool("read_file",path="bookings.json")])
        verify = ("import json\nexec(open('app.py').read())\n"
                  "assert occupied_minutes(json.load(open('bookings.json'))) == 105\n"
                  "open('actual.json','w').write(json.dumps({'minutes':105}))\nprint('verified 105 minutes')")
        script = [tool("delegate_task",assignee="dev",objective="Implement the calculation, verify and document",
                       deliverable_paths=["app.py","README.md","dashboard-checks.md"],remaining_roles=[]),
                  *reads,tool("edit_file",path="app.py",old_text="return 0",new_text="return sum(rows)"),
                  tool("run_python",code=verify),tool("append_file",path="README.md",content=" Results are minutes."),
                  write("dashboard-checks.md","The real fixture totals 105 minutes."),{"text":"Verified."},{"text":"Ready."}]
        detail,provider = self.execute(script,{"parent_run_id":parent["run"]["id"],"intent":"edit","recipient":"lead"},
                                      objective="Upgrade existing project",acceptance_files=["dashboard-checks.md"],
                                      acceptance_checks=[{"kind":"json_equals","path":"actual.json","pointer":"/minutes","expected":105}])
        self.assertEqual(detail["run"]["status"],"completed",detail["diagnostics"])
        for idx in (6,7,8):
            self.assertTrue({"read_file","edit_file","run_python","write_file"} <= set(provider.requests[idx][1]))
        [report] = [e for e in detail["room_events"] if e["kind"]=="member_report"]
        self.assertTrue(report["published"])
        changes={f["path"]:f["change"] for f in report["handoff"]["changed_files"]}
        self.assertEqual(changes,{"README.md":"modified","actual.json":"added","app.py":"modified","dashboard-checks.md":"added"})
        self.assertIn("verified 105 minutes",str(provider.requests[8][0]))

    def test_elastic_budget_funds_implementation_after_discovery_without_spending_elected_minimums(self):
        from miniagent.rao import Budget
        from miniagent.studio_crew import RoomCrew
        room = RoomCrew.__new__(RoomCrew)
        room.budget = Budget(steps_left=99,tokens_left=995000,actions_left=395,seconds_left=3590)
        room.child_checks,room.approved_roles,room.started_roles,room.untapped_shares = {},set(),set(),{}
        room.deadline,room.close_tokens,room.close_seconds = time.monotonic()+3590,12000,60
        plan = {"builder","review","release"}
        first,reserve = room._allocation("builder",plan)
        floors = {k:v.model_copy() for k,v in room.untapped_shares.items()}
        room.started_roles.add("builder")
        room.budget.spend(steps=30,tokens=342000,actions=53,seconds=140)
        implementation,held = room._allocation("builder",plan)
        self.assertGreater(implementation.tokens_left,140000)
        self.assertGreater(implementation.steps_left,15)
        self.assertEqual(room.untapped_shares,floors)
        for field in type(first).model_fields:
            self.assertLess(getattr(floors["review"],field),getattr(first,field))
            self.assertLessEqual(sum(getattr(x,field) for x in [implementation,held,*floors.values()]),getattr(room.budget,field))

    def test_coordinator_can_use_and_retain_public_navigation_before_delegating_then_closes(self):
        group = roster("lead","writer")
        secret_line = "existing_result = 81971"
        source = "# Existing project code and public interface details.\n" * 205 + secret_line + "\n"
        initial,_ = self.execute([write("module.py",source),{"text":"Initial source ready."}],
                                 group, mode="single",objective="Start the project")
        read = tool("read_lines",path="module.py",start_line=206,limit=1)
        script = [tool("find_files",pattern="*.py"), read, read, read,
                  tool("delegate_task",assignee="writer",objective="Write a short guide to the public result",remaining_roles=[]),
                  write("guide.md","The current calculation returns 81971."),{"text":"Guide delivered."},
                  tool("read_file",path="module.py"),{"text":"Delivered."}]
        detail,provider=self.execute(script,{"parent_run_id":initial["run"]["id"],"recipient":"lead","intent":"edit"},
                                     objective="Explain the existing result",acceptance_files=["guide.md"])
        self.assertEqual(detail["run"]["status"],"completed",detail["diagnostics"])
        first, initial_tools = provider.requests[0]
        self.assertIn("find_files",initial_tools)
        self.assertNotIn("run_shell",initial_tools)
        self.assertNotIn("write_file",initial_tools)
        self.assertNotIn(secret_line,str(first))
        self.assertIn("module.py",str(provider.requests[1][0][-1]["content"].split("负责人已核实的公开工作区内容")[1]))
        self.assertIn(secret_line,str(provider.requests[2][0][-1]))
        self.assertIn("read_lines",provider.requests[4][1])
        self.assertIn("分工提醒",str(provider.requests[4][0]))
        member_input = str(provider.requests[5][0])
        self.assertIn("public_source_locations", member_input)
        self.assertIn(secret_line, member_input)
        for _,offered in provider.requests[-2:]:
            self.assertEqual(offered,[])
        self.assertEqual([e["recipient"] for e in detail["room_events"] if e["kind"]=="assignment"],["writer"])

    def test_measured_coordinator_reads_retain_cumulative_ceiling_across_a_failed_handoff(self):
        group = roster("lead","writer")
        initial,_ = self.execute([write("notes.md","First genuine fact.\nSecond genuine fact.\nThird genuine fact.\n"),
                                  {"text":"Notes ready."}],group,mode="single",objective="Start the project")
        read = lambda n: tool("read_lines",path="notes.md",start_line=n,limit=1)
        script = [read(1),read(2),tool("delegate_task",assignee="writer",objective="Write guide",remaining_roles=[]),
                  write("draft.md","Private draft in progress."),{"text":"[BLOCKED] Need final guide"},
                  read(3),assign("writer","Finish the missing guide"),write("guide.md","Final factual guide."),
                  {"text":"Published guide."},{"text":"Guide delivered."}]
        class Metered(RecordingProvider):
            def chat(self,messages,tools=None,temperature=0):
                reply = super().chat(messages,tools,temperature)
                if any(call.name=="read_lines" for call in reply.tool_calls):
                    return reply.model_copy(update={"usage":Usage(prompt_tokens=5000,completion_tokens=100)})
                return reply
        provider=Metered(script)
        with patch("miniagent.studio_service.DeepSeekProvider",return_value=provider):
            run=self.service.start(request("team",conversation={"parent_run_id":initial["run"]["id"],
                                        "recipient":"lead","intent":"edit"},objective="Finish guide",
                                        acceptance_files=["guide.md"],limits={"max_total_tokens":500000}))
            detail=finished(self.service,run["id"])
        self.assertEqual(detail["run"]["status"],"completed",detail["diagnostics"])
        self.assertIn("read_lines",provider.requests[1][1])
        self.assertIn("read_lines",provider.requests[2][1])
        self.assertIn("read_lines",provider.requests[5][1])
        self.assertIn("delegate_task",provider.requests[6][1])
        self.assertIn("read_lines",provider.requests[6][1])
        limits=[e for e in detail["events"] if e["kind"]=="coordinator_intake_limit"]
        self.assertEqual([e["revision"]["tokens"] for e in limits],[10200,15300])
        self.assertEqual([e["decision"]["rounds"] for e in limits],[2,1])
        self.assertLess(limits[-1]["decision"]["tokens"],limits[-1]["decision"]["max_tokens"])
        self.assertGreaterEqual(limits[-1]["revision"]["tokens"],limits[-1]["revision"]["max_tokens"])

    def test_lead_handles_routing_decisions_without_sending_them_to_user(self):
        question = "Should I re-delegate to QA or inspect the source myself?"
        script = [tool("ask_human", question=question, decision_kind="user_preference"),
                  tool("delegate_task", assignee="review", objective="Review business rule", remaining_roles=[]),
                  {"text": "Reviewed the business rule."}, {"text": "Here is the review."}]
        detail, provider = self.execute(script, roster("lead", "review"), objective="Review policy")
        self.assertEqual(detail["run"]["status"], "unverified", detail["diagnostics"])
        self.assertNotIn("ask_human", provider.requests[1][1])
        self.assertFalse(any(e["kind"] == "human_question" for e in detail["room_events"]))
        self.assertIn("团队内部分工", str(provider.requests[1][0]))
        initial_prompt = str(provider.requests[0][0])
        self.assertIn("search_sources", initial_prompt)
        self.assertIn("http_get", initial_prompt)
        self.assertNotIn("http_get", provider.requests[0][1])

    def test_unanswered_member_question_cannot_consume_the_leads_deadline(self):
        service_handler = self.service._human_handler
        observed = []

        def immediate_member_timeout(run_id, role, *, deadline=None):
            if role == "dev":
                observed.append(deadline)
                return service_handler(run_id, role, deadline=time.monotonic() - 1)
            return service_handler(run_id, role, deadline=deadline)

        with patch.object(self.service, "_human_handler", side_effect=immediate_member_timeout):
            script = [tool("delegate_task", assignee="dev", objective="Resolve the issue", remaining_roles=[]),
                      tool("ask_human", question="Which instance can be used?"), {"text": "[BLOCKED] no reply"},
                      {"text": "[BLOCKED] Missing required instance selection."}]
            detail, _ = self.execute(script, roster("lead", "dev"), objective="Diagnose service")
        self.assertTrue(observed and observed[0] is not None)
        self.assertEqual(detail["run"]["status"], "failed", detail["diagnostics"])
        self.assertTrue(any(e["kind"] == "question_closed" for e in detail["room_events"]))
        self.assertFalse(next(e for e in detail["room_events"] if e["kind"] == "member_report")["published"])
        self.assertGreaterEqual(sum(e["kind"] == "step_start" and e["level"] == "goal" for e in detail["events"]), 2)
        self.assertFalse(any(e["kind"] == "cancel_observed" for e in detail["events"]))

    def test_member_human_reply_reaches_lead_and_subsequent_member(self):
        group = roster("fund_manager","research","risk","marketing")
        script = [assign("marketing","clarify client horizon"), tool("ask_human", question="When do you need the money?"),
                  {"text":"Client responded."}, assign("risk","review liquidity"),
                  {"text":"Liquidity reviewed."}, {"text":"Proposal."}]
        provider = RecordingProvider(script)
        answer = "I need the funds in two years."
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.service.start(request("team", conversation=group, objective="Prepare simulated proposal",
                                             limits={"max_seconds":10}))
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                detail = self.service.detail(run["id"])
                if detail["pending_questions"]:
                    break
                time.sleep(.01)
            [pending] = detail["pending_questions"]
            self.assertEqual(pending["role"], "marketing")
            self.service.reply_human(run["id"], pending["request_id"], answer)
            detail = finished(self.service, run["id"])
        self.assertEqual(detail["run"]["status"], "unverified",detail["diagnostics"])
        self.assertIn(answer, str(provider.requests[3][0]))
        self.assertIn(answer, str(provider.requests[4][0]))
