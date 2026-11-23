"""The Mid_Test comparison harness, driven offline with a scripted model."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from miniagent.bench import Bench, solo_member
from miniagent.cli import _template
from tests.helpers import FakeProvider, call, reply, settings


class BenchTest(unittest.TestCase):
    def test_three_arms_run_both_tasks_and_keep_scenario_one_exclusive(self):
        templates = {"scenario_1": _template("scenario_1"), "scenario_2": _template("scenario_2")}
        active: dict[str, int] = {}
        overlap = []
        lock = threading.Lock()
        seen = {}

        def respond(agent, messages, tools, options):
            names = [t.name for t in tools or []]
            system = messages[0]["content"]
            scenario = "scenario_1" if "Software" in system or "Technical Lead" in system else "scenario_2"
            last = messages[-1]
            if last["role"] == "user" and ("# Your task" in last["content"] or "# Your next" in last["content"]):
                with lock:
                    active[scenario] = active.get(scenario, 0) + 1
                    if scenario == "scenario_1" and active[scenario] > 1:
                        overlap.append(agent)
                time.sleep(0.02)
                with lock:
                    active[scenario] -= 1
            if agent == "agent":
                seen["solo_prompt"] = system
                seen.setdefault("solo_tools", names)
            if "delegate_task" in names:
                seen.setdefault("lead_tools", set()).update(names)
                seen.setdefault("delegate_props", next(t for t in tools if t.name == "delegate_task").parameters["properties"])
                if not any(m["role"] == "tool" for m in messages[2:]):
                    member = [m for m in ("dev", "reporter") if f"`{m}`" in system][0]
                    return reply("", call("delegate_task", assignee=member, task="Write notes.md for this task."))
                return reply("Team delivered notes.md.")
            if not any(m["role"] == "tool" for m in messages[2:]) or messages[-1]["role"] == "user":
                return reply("", call("write_file", path="notes.md", content=f"notes by {agent}\n"))
            return reply(f"{agent} wrote notes.md")

        out = Path(tempfile.mkdtemp()) / "bench"
        bench = Bench(settings(Path(tempfile.mkdtemp())), templates, ["team", "team-classic", "solo"], out,
                      solo_max_steps=7, provider_factory=lambda _s: FakeProvider(respond))
        meta = bench.run(["scenario_1", "scenario_2"])

        rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 12)
        self.assertTrue(all(r["status"] == "completed" for r in rows), rows)
        self.assertEqual(overlap, [], "scenario 1 never runs in two arms at once")
        team = [r for r in rows if r["arm"] == "team"]
        self.assertTrue(all(r["delegations"] == 1 for r in team))
        solo = [r for r in rows if r["arm"] == "solo"]
        self.assertTrue(all(r["delegations"] == 0 for r in solo))
        self.assertIn("Test Engineer", seen["solo_prompt"])
        self.assertIn("review_loop", seen["lead_tools"])
        self.assertNotIn("delegate_task", seen["solo_tools"])
        self.assertTrue((out / "solo" / "scenario_2-task2" / "project" / "notes.md").exists())
        self.assertTrue((out / "team" / "scenario_1-task1" / "chat.jsonl").exists())
        self.assertIn("| team-classic | 4/4 |", (out / "summary.md").read_text())
        classic = [r for r in rows if r["arm"] == "team-classic"]
        self.assertTrue(all(r["delegations"] == 1 for r in classic))
        self.assertIsNone(meta["balance_before"], "no key, no balance request")
        solo_task = json.loads(next((out / "solo" / "runs" / "groups").glob("*/tasks/*/task.json")).read_text())
        self.assertLessEqual(solo_task["metrics"]["steps"], 7)

    def test_low_balance_stops_before_a_task_starts(self):
        calls = []
        provider = FakeProvider(lambda *args: calls.append(args) or reply("done"))
        out = Path(tempfile.mkdtemp()) / "bench"
        bench = Bench(settings(Path(tempfile.mkdtemp())), {"scenario_2": _template("scenario_2")}, ["solo"], out,
                      provider_factory=lambda _s: provider, min_balance=5)
        with mock.patch("miniagent.bench.balance", return_value=[{"currency": "CNY", "total_balance": "1.20"}]):
            bench.run(["scenario_2"])
        rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
        self.assertEqual([r["status"] for r in rows], ["skipped_low_balance"])
        self.assertEqual(calls, [], "no model request was made")

    def test_only_the_selected_tasks_run(self):
        provider = FakeProvider(lambda agent, messages, tools, options: reply("done"))
        out = Path(tempfile.mkdtemp()) / "bench"
        Bench(settings(Path(tempfile.mkdtemp())), {"scenario_2": _template("scenario_2")}, ["solo"], out,
              provider_factory=lambda _s: provider, tasks=[1]).run(["scenario_2"])
        rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
        self.assertEqual([(r["task"], r["status"]) for r in rows], [(1, "completed")])

    def test_solo_member_carries_every_role(self):
        template = _template("scenario_3")
        member = solo_member(template)
        for role in template["members"]:
            self.assertIn(role["name"], member["prompt"])


if __name__ == "__main__":
    unittest.main()
