"""Routed files and summaries in the assistant retain the producer's identity."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_route_results import delivery_catalog, imported_review_candidate, project_dispatches, read_delivery
from miniagent.studio_service import StudioError, StudioService
from tests.test_studio_conversations import RecordingProvider
from tests.test_studio_superagent import message, tool


def dispatch(target, objective, **options):
    return tool("dispatch_studio_task", target_kind="role", target_id=target, required_participants=[target],
                objective=objective, **options)


class ReturnedFilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = StudioService(self.root / "runs", Settings(api_key="example-credential"), max_workers=1)
        self.addCleanup(self.service.close)

    def routed(self, source, child, *, parent=None, workdir=None):
        payload = message("按这次的要求安排分工。", parent)
        if workdir:
            payload["workspace_path"] = str(workdir)
        providers = [RecordingProvider(source), RecordingProvider(child)]
        with patch("miniagent.studio_service.DeepSeekProvider", side_effect=providers):
            rid = self.service.send_superagent(payload)["id"]
            for _ in range(500):
                turns = self.service.detail(rid)["conversation"]["turns"]
                latest = turns[-1].get("dispatches", [])
                if latest and latest[0]["status"] in {"completed", "unverified", "failed"}:
                    return rid, latest[0], providers
                time.sleep(.01)
        self.fail("No terminal dispatch receipt")

    def test_running_dispatch_projects_open_model_step_after_a_tool_error(self):
        rid, row, _ = self.routed([dispatch("worker", "Write a note"), {"text": "已交接"}], [{"text": "Done"}])
        child = self.service.runs_dir / row["child_run_id"]
        meta_file = child / "studio.json"
        meta = json.loads(meta_file.read_text())
        meta["state"] = "running"
        meta_file.write_text(json.dumps(meta))
        common = {"task_id": child.name, "attempt_id": "a", "run_id": "r"}
        events = [{**common, "kind": "tool_result", "step": 1, "operation": "read_file", "action_id": "one",
                   "agent": "worker", "ok": False},
                  {**common, "kind": "step_start", "step": 2, "phase": "work"}]
        self.service._active.add(child.name)
        try:
            with patch.object(self.service, "_events", return_value=(events, False)):
                entry, = project_dispatches(self.service, rid)
                self.assertEqual(entry["status"], "running")
                self.assertNotIn("current_call", entry)
                phase, = entry["current_activity"]
                self.assertEqual((phase["role"], phase["step"]), ("worker", 2))
                events.append({**common, "kind": "tool_start", "step": 2, "tool": "write_file", "agent": "worker",
                               "action_id": "two"})
                entry, = project_dispatches(self.service, rid)
                self.assertEqual(entry["current_call"]["tool"], "write_file")
            meta["studio_dispatch"]["source_run_id"] = "unrelated"
            meta_file.write_text(json.dumps(meta))
            entry, = project_dispatches(self.service, rid)
            self.assertEqual(entry["status"], "unknown")
            self.assertIsNone(entry["child_run_id"])
        finally:
            self.service._active.discard(child.name)

    def test_next_turn_can_forward_frozen_output_and_observe_result(self):
        parent, produced, _ = self.routed(
            [dispatch("worker", "写一份诊断", acceptance_files=["checks/report.md"]), {"text": "已交接。"}],
            [tool("write_file", path="checks/report.md", content="机号 LAB-314，已根据原始记录核对。"), {"text": "已核实机号。"}])
        self.assertEqual(produced["status"], "completed")
        original, = produced["artifacts"]
        child = self.service.runs_dir / produced["child_run_id"]
        (child / "agents/worker/checks/report.md").write_text("已改动的工作副本")
        self.assertEqual(self.service.artifact(produced["child_run_id"], original["path"])["content"], "机号 LAB-314，已根据原始记录核对。")
        second, returned, providers = self.routed(
            [dispatch("reviewer", "核查旧报告", input_files=[original["ref"]], acceptance_files=["review.md"]),
             {"text": "已发给评审员。"}],
            [tool("read_file", path="checks/report.md"),
             tool("write_file", path="review.md", content="LAB-314 符合记录。"), {"text": "已复核。"}], parent=parent)
        self.assertEqual(returned["status"], "completed", returned)
        self.assertEqual([file["name"] for file in returned["artifacts"]], ["review.md"])
        self.assertIn("机号 LAB-314", json.dumps(providers[1].requests[-1][0], ensure_ascii=False))
        context = json.dumps(providers[0].requests[0][0], ensure_ascii=False)
        self.assertIn(original["ref"], context)
        self.assertIn("已核实机号", context)
        self.assertNotEqual(second, parent)
        author = imported_review_candidate(self.service, SimpleNamespace(path=self.service.runs_dir / returned["child_run_id"]),
                                          "checks/report.md", {"checks/report.md": original["sha256"]}, set())
        self.assertEqual((author["role"], author["task_id"]), ("worker", produced["child_run_id"]))
        chosen = self.service.runs_dir / second
        self.assertEqual(delivery_catalog(self.service, self.service._meta(chosen), "checks/report")["files"][0]["ref"],
                         original["ref"])
        self.assertIn("LAB-314", read_delivery(self.service, chosen, original["ref"])["content"])

    def test_plain_input_path_resolves_to_an_unambiguous_prior_delivery(self):
        first, delivered, _ = self.routed(
            [dispatch("worker", "提供项目报告"), {"text": "已发送。"}],
            [tool("write_file", path="reports/first.md", content="可读的原版报告。"), {"text": "已交付。"}])
        _, forwarded, _ = self.routed(
            [dispatch("reviewer", "再读原件", input_files=["reports/first.md"]), {"text": "已发给评审员。"}],
            [tool("read_file", path="reports/first.md"), {"text": "已读。"}], parent=first)
        self.assertEqual(forwarded["status"], "unverified", forwarded)
        self.assertEqual(forwarded["input_files"], [delivered["artifacts"][0]["ref"]])
        self.assertEqual(forwarded["artifacts"], [])

    def test_forged_source_or_changed_frozen_bytes_do_not_enter_target(self):
        first, produced, _ = self.routed(
            [dispatch("worker", "输出检查清单"), {"text": "已安排。"}],
            [tool("write_file", path="public.md", content="public"), {"text": "检查完成。"}])
        original, = produced["artifacts"]
        changed = self.service.runs_dir / produced["child_run_id"] / original["path"]
        # Public href uses "handoffs/<id>/<name>"; storage also has a project directory.
        (changed.parent / "project" / changed.name).write_text("corrupted")
        with self.assertRaises(StudioError):
            self.service.artifact(produced["child_run_id"], original["path"])
        second, refused, _ = self.routed(
            [dispatch("reviewer", "核对报告", input_files=[original["ref"]]), {"text": "已登记。"}], [], parent=first)
        self.assertEqual(refused["status"], "failed")
        self.assertIsNone(refused["child_run_id"])
        different = self.root / "different-project"
        different.mkdir()
        _, isolated, _ = self.routed(
            [dispatch("reviewer", "跨项目尝试", input_files=[original["ref"]]), {"text": "已登记。"}],
            [], parent=second, workdir=different)
        self.assertEqual(isolated["status"], "failed")
        self.assertIn("本项目", isolated["error"])
        self.assertIsNone(isolated["child_run_id"])
