"""Checks on assignments, parallel hand-offs, review loops, reviewer settings, the lead's plan and lessons.

Each test drives the normal Studio entry (post a message to a group, wait for the
task) with an offline model, as a user would, and inspects what the lead, the
members and the chat saw.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from miniagent.config import TeamLimits
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings, tool_results
from tests.test_team import TEAM, finished

TEAM4 = TEAM + [{"id": "release", "name": "Release Manager", "prompt": "Ships releases."}]


def kinds(studio, gid, kind):
    return [e for e in studio.group(gid).chat() if e["kind"] == kind]


class CollaborationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def studio(self, respond, members=TEAM, factory=None, **changes):
        provider = respond if isinstance(respond, FakeProvider) else FakeProvider(respond)
        studio = Studio(settings(self.tmp, **changes), provider_factory=factory or (lambda _s: provider))
        group = studio.create_group("Team", members, "lead", "autonomous")
        return studio, group["id"], provider

    # 1 -------------------------------------------------------------------
    def test_checks_run_after_the_assignment_and_reach_the_lead(self):
        seen = {}
        checks = [{"kind": "exists", "path": "app.py"},
                  {"kind": "contains", "path": "app.py", "text": "def add"},
                  {"kind": "command", "command": "python3 -c \"import app; assert app.add(0.5, 0.5) == 1.0\""}]
        script = Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Write app.py with add(a, b).",
                                 deliverables=["app.py"], checks=checks)),
                  lambda m: (seen.__setitem__("result", tool_results(m)[-1]), reply("done"))[1]],
            dev=[reply("", call("write_file", path="app.py", content="def add(a, b):\n    return int(a) + int(b)\n")),
                 reply("Wrote app.py.")])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Build an adder", queue=True)
        task = finished(studio, gid)
        result = seen["result"]
        self.assertIn("Checks run by Studio on the published project (information, not a gate): 2/3 passed", result)
        self.assertIn("- ✓ app.py exists", result)
        self.assertIn("- ✗ command", result)
        self.assertIn("AssertionError", result, "the failing command's output is attached")
        self.assertEqual(task["status"], "completed", "a failed check does not block delivery")
        report = kinds(studio, gid, "report")[0]
        self.assertEqual((report["checks"]["passed"], report["checks"]["total"]), (2, 3))
        self.assertEqual(task["assignments"][0]["checks"], {"passed": 2, "total": 3})

    def test_invalid_checks_are_refused_before_any_work(self):
        script = Script(lead=[reply("", call("delegate_task", assignee="dev", task="Write app.py with add(a, b).",
                                             checks=[{"kind": "exists", "path": "../secret"}])),
                              lambda m: reply("lead saw: " + tool_results(m)[-1][:40])])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "go", queue=True)
        task = finished(studio, gid)
        self.assertTrue(task["final_answer"].startswith("lead saw: Error: Invalid checks"))
        self.assertEqual(task["delegations"], 0)

    # 2 -------------------------------------------------------------------
    def test_assignments_marked_independent_run_at_the_same_time_then_check_each_other(self):
        barrier = threading.Barrier(2, timeout=5)
        follow_ups = {}

        def member(name, path):
            def first(messages):
                barrier.wait()  # both members must be inside their runs at once
                return reply("", call("write_file", path=path, content=f"by {name}\n"))
            def check(messages):
                follow_ups[name] = messages[-1]["content"]
                return reply("No change needed: nothing in my file depends on theirs.")
            return [first, reply(f"{name} wrote {path}"), check]

        lead_calls = [call("delegate_task", assignee="dev", task="Write the backend in api/server.py.",
                           deliverables=["api/"], independent=True),
                      call("delegate_task", assignee="qa", task="Write the test plan in docs/test-plan.md.",
                           deliverables=["docs/test-plan.md"], independent=True)]
        seen = {}
        script = Script(lead=[reply("", *lead_calls),
                              lambda m: (seen.__setitem__("results", tool_results(m)), reply("done"))[1]],
                        dev=member("dev", "api/server.py"), qa=member("qa", "docs/test-plan.md"))
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Start the project", queue=True)
        task = finished(studio, gid)
        self.assertEqual([r.splitlines()[0] for r in seen["results"]],
                         ["Report from Developer (dev) — status: completed",
                          "Report from Tester (qa) — status: completed"])
        self.assertEqual(task["metrics"]["parallel_batches"], 1)
        root = studio.group(gid).project.root
        self.assertTrue((root / "api/server.py").exists() and (root / "docs/test-plan.md").exists())
        # Each saw the other's actual result before reporting.
        self.assertIn("### Tester (completed)", follow_ups["dev"])
        self.assertIn("docs/test-plan.md (added)", follow_ups["dev"])
        self.assertIn("qa wrote docs/test-plan.md", follow_ups["dev"])
        self.assertIn("dev wrote api/server.py", follow_ups["qa"])
        for result in seen["results"]:
            self.assertIn("After seeing the parallel teammates' results: No change needed", result)
            self.assertIn("afterwards checked their results", result)
        checks = [e for e in kinds(studio, gid, "delegation") if e.get("reconcile")]
        self.assertEqual(sorted(e["assignee"] for e in checks), ["dev", "qa"])
        self.assertEqual(task["delegations"], 2, "the follow-up checks are not extra delegations")
        self.assertEqual(task["metrics"]["parallel_checks"], 2)
        self.assertEqual(len(kinds(studio, gid, "report")), 2, "one report per assignment")

    def test_a_wrong_independence_mark_is_corrected_by_the_follow_up(self):
        # The lead says the guide needs nothing from the server, but the guide guessed the port.
        def writer_check(messages):
            text = messages[-1]["content"]
            self.assertIn("listens on port 8080", text)
            return reply("", call("write_file", path="docs/guide.md", content="Open http://localhost:8080\n"))

        lead_calls = [call("delegate_task", assignee="dev", task="Build the web server in app/server.py.",
                           deliverables=["app/server.py"], independent=True),
                      call("delegate_task", assignee="qa", task="Write the user guide in docs/guide.md.",
                           deliverables=["docs/guide.md"], independent=True)]
        seen = {}
        script = Script(
            lead=[reply("", *lead_calls), lambda m: (seen.__setitem__("results", tool_results(m)), reply("done"))[1]],
            dev=[reply("", call("write_file", path="app/server.py", content="PORT = 8080\n")),
                 reply("The server listens on port 8080."),
                 reply("No change needed: the guide does not affect the server.")],
            qa=[reply("", call("write_file", path="docs/guide.md", content="Open http://localhost:3000\n")),
                reply("Wrote the guide; I assumed port 3000."),
                writer_check,
                reply("Updated docs/guide.md: the port is 8080, not 3000.")])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Ship the server and its guide", queue=True)
        finished(studio, gid)
        root = studio.group(gid).project.root
        self.assertEqual((root / "docs/guide.md").read_text(), "Open http://localhost:8080\n")
        qa_result = seen["results"][1]
        self.assertIn("I assumed port 3000.", qa_result)
        self.assertIn("After seeing the parallel teammates' results: Updated docs/guide.md: the port is 8080", qa_result)
        self.assertIn("docs/guide.md (added)", qa_result)

    def test_recovery_after_a_crash_keeps_the_original_assignment_outstanding(self):
        from types import SimpleNamespace
        from miniagent import recovery

        tmp = self.tmp
        room = SimpleNamespace(
            group=SimpleNamespace(kind="team"), cleared_seq=0, task_dir=lambda _tid: tmp,
            chat=lambda: [
                {"kind": "delegation", "task_id": "t1", "assignee": "dev", "task": "Write api/server.py.",
                 "deliverables": ["api/"]},
                {"kind": "delegation", "task_id": "t1", "assignee": "dev", "reconcile": True,
                 "task": "While you worked, these teammates finished…"}])
        record = {"id": "t1", "status": "interrupted", "text": "go"}
        outstanding = recovery.handoff(room, record)["outstanding_assignments"]
        self.assertEqual(outstanding, [{"task_id": "t1", "assignee": "dev", "task": "Write api/server.py.",
                                        "deliverables": ["api/"], "follow_up_check_started": True}])

    def test_a_concurrent_batch_gets_one_thread_per_call(self):
        # Parallel hand-offs wait for each other before their follow-up, so a call queued behind the
        # others in a small thread pool would never start. Eight calls must all be running at once.
        from miniagent.agent import Agent
        from miniagent.config import Limits
        from miniagent.sandbox import Sandbox
        from miniagent.schemas import ToolResult
        from miniagent.tools import Tool, ToolRegistry

        barrier = threading.Barrier(8, timeout=5)
        def waiting(_ctx, args):
            barrier.wait()
            return ToolResult(ok=True, content="done " + args["name"])
        registry = ToolRegistry([Tool("work", "", {"type": "object", "properties": {"name": {"type": "string"}}},
                                      waiting, concurrent=lambda batch: True)])
        calls = [call("work", name=str(i)) for i in range(8)]
        provider = FakeProvider(Script(worker=[reply("", *calls), reply("all done")]))
        agent = Agent("worker", provider, registry, Sandbox(self.tmp, "worker"), settings(self.tmp),
                      limits=Limits(max_steps=12, max_tool_calls_per_step=8),
                      system_prompt="You are Worker (id `worker`).")
        self.assertEqual(agent.run("work").status, "completed")
        self.assertEqual(tool_results(provider.requests[-1]["messages"]), [f"done {i}" for i in range(8)])

    def test_an_assignment_marked_not_independent_keeps_the_response_in_order(self):
        order = []

        def member(name, path):
            def first(messages):
                order.append(name + " start")
                return reply("", call("write_file", path=path, content=name))
            def last(messages):
                order.append(name + " end")
                return reply(f"{name} done")
            return [first, last]

        script = Script(lead=[reply("", call("delegate_task", assignee="dev", task="Write the backend in api/server.py.",
                                             deliverables=["api/"]),
                                    call("delegate_task", assignee="qa", task="Write the test plan in docs/plan.md.",
                                         deliverables=["docs/plan.md"], independent=False)),
                              reply("done")],
                        dev=member("dev", "api/server.py"), qa=member("qa", "docs/plan.md"))
        studio, gid, _ = self.studio(script)
        studio.post(gid, "go", queue=True)
        task = finished(studio, gid)
        self.assertEqual(task["metrics"]["parallel_batches"], 0)
        self.assertEqual(order, ["dev start", "dev end", "qa start", "qa end"])
        self.assertFalse([e for e in kinds(studio, gid, "delegation") if e.get("reconcile")])

    def test_a_brief_that_names_another_assignment_runs_after_it(self):
        script = Script(lead=[reply("", call("delegate_task", assignee="dev", task="Write the backend in api/server.py.",
                                             deliverables=["api/server.py"], independent=True),
                                    call("delegate_task", assignee="qa", task="Test api/server.py and write qa/report.md.",
                                         deliverables=["qa/report.md"], independent=True)),
                              reply("done")],
                        dev=[reply("", call("write_file", path="api/server.py", content="x")), reply("dev done")],
                        qa=[reply("", call("write_file", path="qa/report.md", content="ok")), reply("qa done")])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "go", queue=True)
        self.assertEqual(finished(studio, gid)["metrics"]["parallel_batches"], 0)

    def test_a_refused_assignment_in_a_parallel_batch_does_not_hold_up_the_other(self):
        seen = {}
        script = Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Write the backend in api/server.py.",
                                 deliverables=["api/"], independent=True),
                        call("delegate_task", assignee="qa", task="Write the test plan in docs/plan.md.",
                             deliverables=["docs/plan.md"], independent=True,
                             checks=[{"kind": "exists", "path": "../secret"}])),
                  lambda m: (seen.__setitem__("results", tool_results(m)), reply("done"))[1]],
            dev=[reply("", call("write_file", path="api/server.py", content="x")), reply("dev done")])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "go", queue=True)
        task = finished(studio, gid, timeout=10)
        self.assertEqual(task["metrics"]["parallel_batches"], 1)
        self.assertIn("dev done", seen["results"][0])
        self.assertIn("did not run (no teammate's assignment ran)", seen["results"][0])
        self.assertIn("Invalid checks", seen["results"][1])

    def test_overlapping_or_undeclared_files_run_one_after_another(self):
        spans = {}

        def member(name, path):
            def first(messages):
                spans[name] = [time.monotonic()]
                time.sleep(0.05)
                return reply("", call("write_file", path=path, content=name))
            def last(messages):
                spans[name].append(time.monotonic())
                return reply(f"{name} done")
            return [first, last]

        script = Script(lead=[reply("", call("delegate_task", assignee="dev", task="Write the user guide.",
                                             deliverables=["docs/"], independent=True),
                                    call("delegate_task", assignee="qa", task="Write the release notes.",
                                         deliverables=["docs/guide.md"], independent=True)),
                              reply("done")],
                        dev=member("dev", "docs/guide.md"), qa=member("qa", "docs/notes.md"))
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Write a guide", queue=True)
        task = finished(studio, gid)
        self.assertEqual(task["metrics"]["parallel_batches"], 0)
        first, second = sorted(spans.values())
        self.assertLessEqual(first[1], second[0], "the second assignment started after the first ended")

    def test_parallel_hand_offs_can_be_switched_off(self):
        tools = {}

        def respond(agent, messages, tool_specs, options):
            if agent == "lead":
                tools["delegate"] = next(t for t in tool_specs if t.name == "delegate_task").description
                return reply("done")
            raise AssertionError(agent)

        studio, gid, _ = self.studio(respond, team=TeamLimits(lead_max_steps=12, parallel_delegations=False))
        studio.post(gid, "go", queue=True)
        finished(studio, gid)
        self.assertNotIn("run at the same time", tools["delegate"])

    # 3 -------------------------------------------------------------------
    def test_review_loop_sends_findings_straight_to_the_author(self):
        seen = {}

        def lead_done(messages):
            seen["lead_results"] = tool_results(messages)
            return reply("Shipped after review.")

        script = Script(
            lead=[reply("", call("review_loop", author="dev", reviewer="qa",
                                 work="dev built app.py with add(a, b); test it against the spec.",
                                 criteria="add() works for ints and floats.", deliverables=["app.py"],
                                 checks=[{"kind": "contains", "path": "app.py", "text": "return a + b"}])),
                  lead_done],
            qa=[reply("VERDICT: CHANGES REQUESTED\n1. add(0.5, 0.5) returns 0; it must return 1.0."),
                lambda m: (seen.__setitem__("qa_round2", m[-1]["content"]), reply("VERDICT: APPROVED\nFloats work now."))[1]],
            dev=[lambda m: (seen.__setitem__("dev_fix", m[-1]["content"]),
                            reply("", call("write_file", path="app.py", content="def add(a, b):\n    return a + b\n")))[1],
                 reply("Fixed add() to keep floats; ran add(0.5, 0.5) -> 1.0.")],
        )
        studio, gid, provider = self.studio(script)
        (studio.group(gid).project.root / "app.py").write_text("def add(a, b):\n    return int(a) + int(b)\n")
        studio.post(gid, "Review and fix the adder", queue=True)
        task = finished(studio, gid)

        self.assertEqual(len(seen["lead_results"]), 1, "the lead received one result for the whole loop")
        result = seen["lead_results"][0]
        self.assertIn("Review loop Developer ⇄ Tester: APPROVED after 2 review round(s).", result)
        self.assertIn("Round 1 — review by Tester: CHANGES REQUESTED; fix by Developer: completed, 1 file(s) changed", result)
        self.assertIn("Round 2 — review by Tester: APPROVED", result)
        self.assertIn("Files changed by the fixes: app.py (modified)", result)
        self.assertIn("1/1 passed", result)
        self.assertIn("add(0.5, 0.5) returns 0", seen["dev_fix"], "the findings went straight to the author")
        self.assertNotIn("VERDICT", seen["dev_fix"].split("# Your task")[-1], "the verdict line is not passed on")
        self.assertIn("Checks run by Studio", seen["qa_round2"], "the reviewer sees the check results of the fix")
        self.assertEqual(task["delegations"], 3)
        self.assertEqual(task["metrics"]["review_assignments"], 2)
        self.assertEqual(task["metrics"]["resumed_assignments"], 1, "qa's second review continues its review session")
        loops = [e.get("loop", {}).get("role") for e in kinds(studio, gid, "delegation")]
        self.assertEqual(loops, ["review", "fix", "review"])

    def test_review_loop_stops_at_the_round_limit(self):
        script = Script(
            lead=[reply("", call("review_loop", author="dev", reviewer="qa", work="Check docs/guide.md for errors.",
                                 max_rounds=2)),
                  lambda m: reply(tool_results(m)[-1].splitlines()[0])],
            qa=[reply("VERDICT: CHANGES REQUESTED\n1. Typo in line 2."), reply("VERDICT: CHANGES REQUESTED\n1. Still a typo.")],
            dev=[reply("Fixed the typo.")],
        )
        studio, gid, _ = self.studio(script)
        studio.post(gid, "go", queue=True)
        task = finished(studio, gid)
        self.assertEqual(task["final_answer"], "Review loop Developer ⇄ Tester: CHANGES REQUESTED after 2 review round(s).")
        self.assertEqual(task["delegations"], 3)

    def test_review_loop_needs_a_different_reviewer(self):
        script = Script(lead=[reply("", call("review_loop", author="dev", reviewer="dev", work="Check app.py please.")),
                              lambda m: reply(tool_results(m)[-1])])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "go", queue=True)
        task = finished(studio, gid)
        self.assertEqual(task["final_answer"], "Error: The reviewer must be someone other than the author.")

    # 4 -------------------------------------------------------------------
    def test_review_assignments_use_the_reviewer_settings(self):
        created = []
        main = FakeProvider(Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Write notes.md about the API.")),
                  reply("", call("delegate_task", assignee="qa", task="Review notes.md for mistakes.", review=True)),
                  reply("done")],
            dev=[reply("Wrote notes.md.")]))
        reviewer = FakeProvider(Script(qa=[reply("VERDICT: APPROVED")]))

        def factory(s):
            created.append((s.model, s.thinking))
            return reviewer if s.thinking == "disabled" else main

        studio, gid, _ = self.studio(main, factory=factory, reviewer_thinking="disabled")
        studio.post(gid, "go", queue=True)
        task = finished(studio, gid)
        self.assertEqual(task["status"], "completed")
        self.assertEqual([r["agent"] for r in reviewer.requests], ["qa"])
        self.assertFalse(main.for_agent("qa"))
        self.assertIn(("deepseek-flash", "disabled"), created)

    def test_reviewer_defaults_to_the_author_settings(self):
        created = []
        provider = FakeProvider(Script(
            lead=[reply("", call("delegate_task", assignee="qa", task="Review notes.md for mistakes.", review=True)),
                  reply("done")],
            qa=[reply("VERDICT: APPROVED")]))
        studio, gid, _ = self.studio(provider, factory=lambda s: created.append(s.thinking) or provider)
        studio.post(gid, "go", queue=True)
        finished(studio, gid)
        self.assertEqual(len(set(created)), 1)
        self.assertTrue(provider.for_agent("qa"))

    # 5 -------------------------------------------------------------------
    def test_lead_plan_is_visible_and_reaches_members(self):
        steps = [{"step": "Write the API", "status": "in_progress", "owner": "dev"},
                 {"step": "Test it", "status": "pending", "owner": "qa"}]
        seen = {}
        script = Script(
            lead=[reply("", call("update_plan", steps=steps, explanation="Two steps.")),
                  reply("", call("delegate_task", assignee="dev", task="Write the API in api.py please.")),
                  reply("", call("update_plan", steps=[]))],
            default=lambda agent, m: reply("lead saw: " + tool_results(m)[-1]) if agent == "lead" else reply("ok"),
            dev=[lambda m: (seen.__setitem__("dev_context", m[1]["content"]), reply("Wrote api.py."))[1]])
        studio, gid, _ = self.studio(script, team=TeamLimits(plan_links=False))
        studio.post(gid, "Build it", queue=True)
        task = finished(studio, gid)
        plans = kinds(studio, gid, "plan")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["steps"], steps)
        self.assertEqual(task["plan"]["steps"], steps)
        self.assertIn("## Team plan (kept by the lead)\n[~] Write the API — dev\n[ ] Test it — qa", seen["dev_context"])
        self.assertTrue(task["final_answer"].startswith("lead saw: Error: Invalid arguments")
                        or task["final_answer"].startswith("lead saw: Error: A plan has 1–20 steps"))

    # 6 -------------------------------------------------------------------
    def test_lessons_are_proposed_and_used_after_approval(self):
        lessons = ('{"lessons": [{"member": "dev", "scope": "group", '
                   '"text": "Run the float cases before reporting; qa found add() truncating floats."}]}')

        def respond(agent, messages, tools, options):
            if messages[0]["content"].startswith("You review a finished multi-agent task"):
                seen["retro"] = messages[1]["content"]
                return reply(lessons)
            if agent == "lead":
                if not tool_results(messages):
                    return reply("", call("delegate_task", assignee="dev", task="Write app.py with add(a, b)."))
                return reply("done")
            seen.setdefault("dev_contexts", []).append(messages[1]["content"])
            return reply("Wrote app.py.")

        seen = {}
        studio, gid, _ = self.studio(respond, team=TeamLimits(lead_max_steps=12, retrospective=True))
        studio.post(gid, "Build an adder", queue=True)
        task = finished(studio, gid)
        event = self._wait_for_lessons(studio, gid)
        self.assertIn("Write app.py with add(a, b).", seen["retro"], "the retrospective sees what happened")
        entry = event["entries"][0]
        self.assertEqual((entry["role"], entry["scope"]), ("dev", "group:" + gid))
        self.assertEqual(studio.roles.history("dev")[-1]["status"], "proposed")
        self.assertNotIn("float cases", seen["dev_contexts"][0])

        studio.roles.revise("dev", entry["id"], "approved")
        studio.post(gid, "Add subtract(a, b) too", queue=True)
        self._wait_for_tasks(studio, gid, 2)
        self.assertIn("Run the float cases before reporting", seen["dev_contexts"][-1], "approved lessons are used")
        self.assertEqual(studio.group(gid).load_task(task["id"])["lessons"], [entry["id"]])

    def test_unusable_retrospective_changes_nothing(self):
        def respond(agent, messages, tools, options):
            if messages[0]["content"].startswith("You review a finished multi-agent task"):
                return reply("I have no JSON for you.")
            return reply("done")

        studio, gid, _ = self.studio(respond, team=TeamLimits(lead_max_steps=12, retrospective=True))
        studio.post(gid, "go", queue=True)
        task = finished(studio, gid)
        time.sleep(0.2)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(kinds(studio, gid, "lessons"), [])

    def _wait_for_lessons(self, studio, gid):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            found = kinds(studio, gid, "lessons")
            if found:
                return found[0]
            time.sleep(0.02)
        raise AssertionError("no lessons event")

    def _wait_for_tasks(self, studio, gid, count):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            tasks = studio.tasks(gid)
            if len(tasks) >= count and all(t["status"] != "running" for t in tasks) and studio.state(gid)["live"] is None:
                return tasks
            time.sleep(0.02)
        raise AssertionError("tasks did not finish")


if __name__ == "__main__":
    unittest.main()
