"""Project checkout/publish, compaction primitives and configuration."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from miniagent.config import Limits, Settings, TeamLimits, load_settings, read_env_file
from miniagent.context import ContextManager, SUMMARY_PREFIX, find_cut_point, prune_tool_outputs
from miniagent.providers import DeepSeekProvider
from miniagent.workspace import Project


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project = Project(self.tmp / "project", self.tmp / "log.jsonl")

    def test_checkout_mirrors_and_publish_applies_add_modify_delete(self):
        (self.project.root / "keep.txt").write_text("a")
        (self.project.root / "old.txt").write_text("old")
        ws = self.tmp / "ws"
        (ws / "stale.txt").parent.mkdir(parents=True)
        (ws / "stale.txt").write_text("from an earlier assignment")
        (ws / "node_modules").mkdir()
        (ws / "node_modules" / "big.js").write_text("cache")
        baseline = self.project.checkout(ws)
        self.assertFalse((ws / "stale.txt").exists())
        self.assertTrue((ws / "node_modules" / "big.js").exists(), "caches are left alone")
        self.assertEqual(set(baseline), {"keep.txt", "old.txt"})
        (ws / "keep.txt").write_text("b")
        (ws / "old.txt").unlink()
        (ws / "src").mkdir()
        (ws / "src" / "new.py").write_text("x = 1\n")
        changes, skipped = self.project.publish(ws, baseline, author="dev", task_id="t1")
        self.assertEqual({(c.path, c.change) for c in changes},
                         {("keep.txt", "modified"), ("old.txt", "deleted"), ("src/new.py", "added")})
        self.assertEqual(skipped, [])
        self.assertEqual((self.project.root / "keep.txt").read_text(), "b")
        self.assertFalse((self.project.root / "old.txt").exists())
        self.assertNotIn("node_modules/big.js", self.project.manifest())
        self.assertEqual(self.project.history()[-1]["author"], "dev")

    def test_concurrent_change_is_reported_as_conflict(self):
        (self.project.root / "a.txt").write_text("1")
        ws = self.tmp / "ws"
        baseline = self.project.checkout(ws)
        (self.project.root / "a.txt").write_text("someone else")
        (ws / "a.txt").write_text("mine")
        conflicts = self.tmp / "conflicts"
        changes, _ = self.project.publish(ws, baseline, author="dev", task_id="t", conflicts_dir=conflicts)
        self.assertTrue(changes[0].conflict)
        self.assertEqual((self.project.root / "a.txt").read_text(), "mine")
        self.assertTrue(changes[0].conflict_copy)
        self.assertEqual(Path(changes[0].conflict_copy).read_text(), "someone else")

    def test_concurrent_delete_saves_replaced_copy(self):
        (self.project.root / "a.txt").write_text("v1")
        ws = self.tmp / "ws"
        baseline = self.project.checkout(ws)
        (self.project.root / "a.txt").write_text("teammate v2")
        (ws / "a.txt").unlink()
        conflicts = self.tmp / "conflicts"
        changes, _ = self.project.publish(ws, baseline, author="dev", task_id="t", conflicts_dir=conflicts)
        self.assertEqual(changes[0].change, "deleted")
        self.assertTrue(changes[0].conflict)
        self.assertEqual(Path(changes[0].conflict_copy).read_text(), "teammate v2")

    def test_publish_and_checkout_handle_file_folder_name_swaps(self):
        # 1. publish: folder -> file
        (self.project.root / "docs").mkdir()
        (self.project.root / "docs" / "a.md").write_text("x")
        ws = self.tmp / "ws1"
        base = self.project.checkout(ws)
        (ws / "docs" / "a.md").unlink()
        (ws / "docs").rmdir()
        (ws / "docs").write_text("now a file")
        changes, skipped = self.project.publish(ws, base, author="a", task_id="t1")
        self.assertEqual(skipped, [])
        self.assertTrue((self.project.root / "docs").is_file())
        self.assertEqual((self.project.root / "docs").read_text(), "now a file")

        # 2. checkout: workspace has folder, project has file
        ws2 = self.tmp / "ws2"
        (ws2 / "docs").mkdir(parents=True)
        (ws2 / "docs" / "stale.md").write_text("old")
        self.project.checkout(ws2)
        self.assertTrue((ws2 / "docs").is_file())
        self.assertEqual((ws2 / "docs").read_text(), "now a file")

        # 3. publish: file -> folder
        base2 = self.project.checkout(ws2)
        (ws2 / "docs").unlink()
        (ws2 / "docs").mkdir()
        (ws2 / "docs" / "b.md").write_text("in folder")
        changes, skipped = self.project.publish(ws2, base2, author="a", task_id="t2")
        self.assertEqual(skipped, [])
        self.assertEqual((self.project.root / "docs" / "b.md").read_text(), "in folder")

        # 4. checkout: workspace has file, project has folder
        ws3 = self.tmp / "ws3"
        ws3.mkdir()
        (ws3 / "docs").write_text("was file")
        self.project.checkout(ws3)
        self.assertEqual((ws3 / "docs" / "b.md").read_text(), "in folder")

    def test_publish_folder_to_file_conflict_preserves_teammate_file(self):
        (self.project.root / "docs").mkdir()
        (self.project.root / "docs" / "a.md").write_text("x")
        ws = self.tmp / "ws"
        base = self.project.checkout(ws)
        # Teammate adds docs/b.md in the project while this member replaces docs/ with a file.
        (self.project.root / "docs" / "b.md").write_text("teammate")
        (ws / "docs" / "a.md").unlink()
        (ws / "docs").rmdir()
        (ws / "docs").write_text("now a file")
        conflicts = self.tmp / "conflicts"
        changes, skipped = self.project.publish(ws, base, author="a", task_id="t", conflicts_dir=conflicts)
        self.assertEqual((self.project.root / "docs" / "b.md").read_text(), "teammate")
        self.assertTrue(any("docs" in item and "conflict" in item for item in skipped))
        saved = list(conflicts.rglob("docs"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_text(), "now a file")

    def test_resolve_refuses_escape(self):
        with self.assertRaises(ValueError):
            self.project.resolve("../log.jsonl")


class ContextTest(unittest.TestCase):
    def history(self, turns: int) -> list[dict]:
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
        for i in range(turns):
            messages.append({"role": "assistant", "content": f"step {i}", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
            messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 4000})
        return messages

    def test_cut_point_never_starts_at_a_tool_result(self):
        messages = self.history(20)
        for keep in (500, 1500, 3000, 9000):
            cut = find_cut_point(messages, 2, keep)
            self.assertNotEqual(messages[cut]["role"], "tool")

    def test_prune_only_touches_old_tool_output(self):
        messages = self.history(6)
        prune_tool_outputs(messages, protect_from=8)
        self.assertLess(len(messages[3]["content"]), 4000)
        self.assertEqual(len(messages[-1]["content"]), 4000)

    def test_summary_replaces_old_span_and_keeps_pinned_messages(self):
        messages = self.history(30)
        manager = ContextManager(compact_at_tokens=5000, keep_recent_tokens=2000)
        result = manager.maybe_compact(messages, None, lambda prompt: ("## Goal\nread files", None))
        self.assertTrue(result.applied)
        self.assertEqual(result.method, "summary")
        self.assertEqual(messages[0]["content"], "sys")
        self.assertEqual(messages[1]["content"], "task")
        self.assertTrue(messages[2]["content"].startswith(SUMMARY_PREFIX))
        DeepSeekProvider._validate_history(messages, require_reasoning=False)
        # A later compaction updates the summary instead of summarizing it.
        messages.extend(self.history(30)[2:])
        seen = {}
        def summarize(prompt):
            seen["prompt"] = prompt[-1]["content"]
            return "## Goal\nupdated", None
        manager.maybe_compact(messages, None, summarize)
        self.assertIn("<previous-summary>", seen["prompt"])
        self.assertEqual(sum(1 for m in messages if (m.get("content") or "").startswith(SUMMARY_PREFIX)), 1)

    def test_summary_sees_the_whole_span_at_a_high_threshold(self):
        # At 500k a compacted span can far exceed the old fixed 400,000-character transcript; it is not cut.
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
        for i in range(120):
            messages.append({"role": "user", "content": f"note {i} " + "x" * 5000})
        messages.append({"role": "user", "content": "latest"})
        manager = ContextManager(compact_at_tokens=150_000, keep_recent_tokens=1000)
        seen = {}
        def summarize(prompt):
            seen["transcript"] = prompt[1]["content"]
            return "## Goal\nsummary", None
        self.assertEqual(manager.maybe_compact(messages, None, summarize).method, "summary")
        self.assertGreater(len(seen["transcript"]), 400_000)
        self.assertNotIn("middle omitted", seen["transcript"])
        self.assertIn("note 60 ", seen["transcript"])

    def test_failed_summary_falls_back_to_pruning(self):
        messages = self.history(30)
        manager = ContextManager(compact_at_tokens=5000, keep_recent_tokens=2000)
        def broken(prompt):
            raise RuntimeError("provider down")
        result = manager.maybe_compact(messages, None, broken)
        self.assertEqual(result.method, "prune")
        self.assertIn("provider down", result.error)
        DeepSeekProvider._validate_history(messages, require_reasoning=False)


class ConfigTest(unittest.TestCase):
    def test_defaults_are_bounded(self):
        settings = Settings()
        self.assertGreater(settings.limits.max_steps, 0)
        self.assertGreater(settings.team.lead_max_steps, 0)
        self.assertGreater(settings.team.max_delegations, 0)
        with self.assertRaises(ValueError):
            TeamLimits(lead_max_steps=0)
        with self.assertRaises(ValueError):
            Limits(max_steps=5000)

    def test_compaction_starts_at_half_the_window_unless_the_window_is_small(self):
        self.assertEqual(Settings.from_env({}).compact_at_tokens, 500_000)
        self.assertEqual(Settings.from_env({"MA_CONTEXT_WINDOW_TOKENS": "128000"}).compact_at_tokens, 128_000 - 16_384)
        self.assertEqual(Settings.from_env({"MA_COMPACT_AT_TOKENS": "110000"}).compact_at_tokens, 110_000)
        with self.assertRaises(ValueError):
            Settings.from_env({"MA_CONTEXT_WINDOW_TOKENS": "128000", "MA_COMPACT_AT_TOKENS": "500000"})

    def test_env_file_literals_and_legacy_unlimited_steps_ignored(self):
        tmp = Path(tempfile.mkdtemp())
        env = tmp / ".env"
        env.write_text("DEEPSEEK_API_KEY=sk-test\nMA_MAX_STEPS=0\nDEEPSEEK_THINKING=disabled\nUNKNOWN=1\n# c\n")
        self.assertEqual(read_env_file(env), {"DEEPSEEK_API_KEY": "sk-test", "DEEPSEEK_THINKING": "disabled"})
        settings = load_settings(env)
        self.assertEqual(settings.api_key, "sk-test")
        self.assertEqual(settings.limits.max_steps, Limits.max_steps)
        env.write_text("DEEPSEEK_API_KEY=$(whoami)\n")
        with self.assertRaises(ValueError):
            read_env_file(env)


if __name__ == "__main__":
    unittest.main()
