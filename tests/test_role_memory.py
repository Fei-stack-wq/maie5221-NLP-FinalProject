import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from miniagent.role_memory import RoleMemory


class RoleMemoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "role-memory.json"
        self.profiles = {r: {"id": r, "prompt": "role " + r} for r in ("lead", "dev", "qa", "release")}

    def memory(self, **kwargs):
        return RoleMemory(self.path, "room-1", self.profiles, enabled=True, **kwargs)

    def record(self, memory, role="dev", published=True):
        memory.remember(role, run_id="run-1", task_id=role + "-task", objective="fixture",
                        summary=role + "-only experience", evidence={"check_signals": ["passed"]},
                        files=["file.py"], published=published)

    def test_four_roles_select_only_own_experience_and_restore_same_snapshot(self):
        memory = self.memory()
        for role in self.profiles:
            self.record(memory, role)
        before = self.path.read_bytes()
        snapshot = json.loads(before)
        restored = self.memory(initial=snapshot)
        for role in self.profiles:
            self.assertIn(role + "-only experience", restored.context(role))
            for other in set(self.profiles) - {role}:
                self.assertNotIn(other + "-only experience", restored.context(role))
        repeat = self.memory(initial=snapshot)
        self.assertEqual(repeat.initial_sha256, restored.initial_sha256)
        self.assertEqual(snapshot["roles"], repeat.data["roles"])

    def test_disabled_or_new_room_resets_and_failed_work_is_not_learned(self):
        memory = self.memory()
        self.record(memory, published=False)
        self.assertEqual(memory.context("dev"), "")
        self.record(memory)
        snapshot = json.loads(self.path.read_bytes())
        disabled = RoleMemory(self.path, "room-1", self.profiles, initial=snapshot)
        self.assertEqual(disabled.context("dev"), "")
        self.assertTrue(all(not rows for rows in disabled.data["roles"].values()))
        fresh = RoleMemory(self.path, "room-2", self.profiles, enabled=True)
        self.assertEqual(fresh.context("dev"), "")
        with self.assertRaises(ValueError):
            RoleMemory(self.path, "room-2", self.profiles, enabled=True, initial=snapshot)
        with self.assertRaises(ValueError):
            self.memory(initial={**snapshot, "profiles": {}})

    def test_memory_is_bounded_and_does_not_expose_configured_secret(self):
        memory = self.memory(redact=lambda value: json.loads(json.dumps(value).replace("sentinel", "[REDACTED]")))
        for i in range(12):
            memory.remember("dev", run_id="r", task_id=str(i), objective="sentinel", summary="s" * 9000,
                            evidence={}, files=[], published=True)
        self.assertEqual(len(memory.data["roles"]["dev"]), 8)
        self.assertNotIn("sentinel", self.path.read_text())
        self.assertLess(len(memory.context("dev")), 10000)
