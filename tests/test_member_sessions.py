"""A member assigned again in one task continues its session instead of starting over.

The failure this covers: in the Mid_Test software run (t-87703c14b591) the
developer was assigned three times. Each assignment began a new conversation,
so after every QA round the developer re-read the whole code base. Its three
runs used about 5.5M, 5.3M and 5.1M prompt tokens.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from miniagent.agent import AgentSession
from miniagent.config import TeamLimits
from miniagent.context import SUMMARY_PREFIX, ContextManager
from miniagent.provider_http import ProviderError
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings, tool_results
from tests.test_team import TEAM, finished


def first_requests(provider: FakeProvider, agent: str) -> list[dict]:
    """The first model request of each run of ``agent`` (a run starts on a user message)."""
    runs = []
    for request in provider.for_agent(agent):
        last = request["messages"][-1]
        if last["role"] == "user" and ("# Your task" in last["content"] or "# Your next assignment" in last["content"]
                                       or "Assignment from" in last["content"]):
            runs.append(request)
    return runs


class MemberSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def studio(self, respond, **changes):
        provider = FakeProvider(respond)
        studio = Studio(settings(self.tmp, **changes), provider_factory=lambda _s: provider)
        group = studio.create_group("Team", TEAM, "lead", "autonomous")
        return studio, group["id"], provider

    def test_reassigned_member_continues_with_what_changed_since(self):
        seen = {}

        def lead_after_fix(messages):
            seen["fix_result"] = tool_results(messages)[-1]
            return reply("Delivered app.py; qa found an edge case and dev fixed it.")

        script = Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Write app.py with add(a, b).",
                                 deliverables=["app.py"])),
                  reply("", call("delegate_task", assignee="qa", task="Test app.py and report problems.")),
                  reply("", call("delegate_task", assignee="dev",
                                 task="Fix the finding from qa: add() must accept floats.")),
                  lead_after_fix],
            dev=[reply("", call("write_file", path="app.py", content="def add(a, b):\n    return int(a) + int(b)\n")),
                 reply("Wrote app.py with add()."),
                 reply("", call("write_file", path="app.py", content="def add(a, b):\n    return a + b\n")),
                 reply("Fixed add() for floats.")],
            qa=[reply("", call("write_file", path="tests/test_app.py", content="assert add(0.5, 0.5) == 1.0\n")),
                reply("add(0.5, 0.5) returns 0, expected 1.0. Test in tests/test_app.py.")],
        )
        studio, gid, provider = self.studio(script)
        studio.post(gid, "Build an adder", queue=True)
        task = finished(studio, gid)

        self.assertEqual(task["status"], "completed")
        runs = first_requests(provider, "dev")
        self.assertEqual(len(runs), 2)
        first, second = runs[0]["messages"], runs[1]["messages"]
        self.assertEqual(second[0], first[0], "the system prompt is unchanged")
        self.assertEqual(second[1], first[1], "the first assignment is still in the history")
        self.assertTrue(any(m.get("content") == "Wrote app.py with add()." for m in second),
                        "the earlier report is remembered")
        self.assertTrue(any(m["role"] == "tool" for m in second), "earlier tool results are remembered")
        note = second[-1]["content"]
        self.assertIn("# Your next assignment", note)
        self.assertIn("add() must accept floats", note)
        self.assertIn("## Team conversation since your last assignment", note)
        self.assertIn("add(0.5, 0.5) returns 0", note, "qa's report reaches the continued developer")
        self.assertIn("- tests/test_app.py (added)", note)
        self.assertNotIn("app.py (changed)", note, "the member's own file is not reported as someone else's change")
        self.assertIn("Background processes from your previous assignment", note)
        self.assertIn("Session: continued their earlier session in this task.", seen["fix_result"])
        self.assertEqual([a["resumed"] for a in task["assignments"]], [False, False, True])
        self.assertEqual(task["metrics"]["resumed_assignments"], 1)
        self.assertEqual((studio.group(gid).project.root / "app.py").read_text(),
                         "def add(a, b):\n    return a + b\n")

    def test_fresh_starts_a_clean_session(self):
        script = Script(
            lead=[reply("", call("delegate_task", assignee="qa", task="Review the plan in plan.md for gaps.")),
                  reply("", call("delegate_task", assignee="qa", task="Review plan.md again, independently.",
                                 fresh=True)),
                  lambda m: reply("lead saw: " + next(line for line in tool_results(m)[-1].splitlines()
                                                      if line.startswith("Session:")))],
            qa=[reply("Plan has two gaps."), reply("Independent review: one gap.")],
        )
        studio, gid, provider = self.studio(script)
        studio.post(gid, "Check the plan", queue=True)
        task = finished(studio, gid)
        runs = first_requests(provider, "qa")
        self.assertEqual(len(runs[1]["messages"]), 2)
        self.assertIn("# Your task", runs[1]["messages"][1]["content"])
        self.assertEqual(task["final_answer"], "lead saw: Session: new session.")
        self.assertEqual(task["metrics"]["resumed_assignments"], 0)

    def test_resume_can_be_switched_off(self):
        tools_seen = {}

        def respond(agent, messages, tools, options):
            if agent == "lead":
                spec = next(t for t in tools if t.name == "delegate_task")
                tools_seen["delegate"] = spec.parameters["properties"]
                return lead(messages)
            return reply("done: " + str(len(messages)))

        lead_steps = iter([
            reply("", call("delegate_task", assignee="dev", task="Write the first module please.")),
            reply("", call("delegate_task", assignee="dev", task="Write the second module please.")),
            reply("finished"),
        ])
        lead = lambda _messages: next(lead_steps)  # noqa: E731
        studio, gid, provider = self.studio(respond, team=TeamLimits(lead_max_steps=12, resume_members=False))
        studio.post(gid, "go", queue=True)
        task = finished(studio, gid)
        self.assertNotIn("fresh", tools_seen["delegate"])
        self.assertTrue(all(len(r["messages"]) == 2 for r in first_requests(provider, "dev")))
        self.assertNotIn("continues their earlier session", provider.for_agent("lead")[0]["messages"][0]["content"])
        self.assertEqual(task["metrics"]["resumed_assignments"], 0)

    def test_member_run_that_failed_starts_fresh_next_time(self):
        def dev_fails(messages):
            raise ProviderError("boom", code="network_error")

        script = Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Write the module please.")),
                  reply("", call("delegate_task", assignee="dev", task="Try writing the module again.")),
                  reply("ok")],
            dev=[reply("", call("list_dir", path=".")), dev_fails, reply("Wrote it this time.")],
        )
        studio, gid, provider = self.studio(script)
        studio.post(gid, "go", queue=True)
        task = finished(studio, gid)
        runs = first_requests(provider, "dev")
        self.assertEqual(len(runs), 2)
        self.assertEqual(len(runs[1]["messages"]), 2, "a broken history is not continued")
        self.assertEqual([a["status"] for a in task["assignments"]], ["error", "completed"])
        self.assertEqual([a["resumed"] for a in task["assignments"]], [False, False])


class SessionUnitTest(unittest.TestCase):
    def test_only_a_cleanly_finished_conversation_is_resumable(self):
        session = AgentSession()
        self.assertFalse(session.resumable)
        session.context = ContextManager(1000, 100)
        session.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"},
                            {"role": "assistant", "content": None,
                             "tool_calls": [{"id": "c1", "type": "function",
                                             "function": {"name": "list_dir", "arguments": "{}"}}]},
                            {"role": "tool", "tool_call_id": "c1", "content": "a.py"}]
        self.assertFalse(session.resumable)
        session.messages.append({"role": "assistant", "content": "done"})
        self.assertTrue(session.resumable)

    def test_compaction_keeps_the_current_assignment_verbatim(self):
        manager = ContextManager(compact_at_tokens=400, keep_recent_tokens=60)
        big = "x " * 400
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "first assignment"},
                    {"role": "assistant", "content": "first report " + big}]
        current = {"role": "user", "content": "# Your next assignment\nfix the float bug"}
        messages.append(current)
        manager.anchor(current)
        messages += [{"role": "assistant", "content": "working " + big},
                     {"role": "user", "content": "keep going"},
                     {"role": "assistant", "content": "still working " + big},
                     {"role": "user", "content": "nearly done"}]
        transcripts = []

        def summarize(prompt):
            transcripts.append(prompt[1]["content"])
            return "Goal: fix", None

        result = manager.maybe_compact(messages, None, summarize)
        self.assertEqual(result.method, "summary")
        self.assertTrue(messages[2]["content"].startswith(SUMMARY_PREFIX))
        self.assertIs(messages[3], current, "the anchored assignment follows the summary verbatim")
        self.assertNotIn("fix the float bug", transcripts[0], "it is not summarized as well")


if __name__ == "__main__":
    unittest.main()
