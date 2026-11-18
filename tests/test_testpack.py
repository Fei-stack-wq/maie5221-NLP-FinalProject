import copy
import io
import json
from pathlib import Path
import unittest
import zipfile
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.studio_service import StudioService
from miniagent.testpack import assess_files, load_pack, run_pack
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import tool


PACK = Path(__file__).resolve().parents[1] / "examples/testpack-draft.json"


class TestpackSchemaTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("testpack-schema")

    def tearDown(self):
        cleanup(self.root)

    def test_pack_explicitly_declares_missing_originals_and_four_roles(self):
        pack = load_pack(PACK)
        self.assertEqual(pack["source_kind"], "derived_document_summary")
        self.assertEqual(len(pack["scenarios"]), 3)
        self.assertTrue(all(len(s["profiles"]) == 4 and len(s["tasks"]) == 2 for s in pack["scenarios"]))

    def test_invalid_paths_and_role_roster_are_rejected(self):
        base = load_pack(PACK)
        for mutation in (lambda p: p["scenarios"][0]["tasks"][0].update(outputs=["../stale.txt"]),
                         lambda p: p["scenarios"][0].update(profiles=p["scenarios"][0]["profiles"][:3]),
                         lambda p: p.update(source_kind="original_testpack")):
            payload = copy.deepcopy(base)
            mutation(payload)
            file = self.root / "pack.json"
            file.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_pack(file)

    def test_missing_file_and_unchanged_prior_output_fail(self):
        task = {"outputs": ["report.md"]}
        self.assertFalse(assess_files(task, {})["passed"])
        self.assertFalse(assess_files(task, {"report.md": b"old"}, {"report.md": b"old"})["passed"])
        self.assertTrue(assess_files(task, {"report.md": b"updated"}, {"report.md": b"old"})["passed"])

    def test_portfolio_checks_each_ratio_total_and_nonfinite_values(self):
        task = {"outputs": ["portfolio.json"], "validator": "portfolio"}
        good = {"total": 1000, "assets": [{"weight": 40, "amount": 400}, {"weight": 60, "amount": 600}]}
        run = lambda data: assess_files(task, {"portfolio.json": json.dumps(data).encode()})
        self.assertTrue(run(good)["passed"])
        for value in ({"total": 1000, "assets": [{"weight": 100, "amount": 900}]},
                      {"total": 1000, "assets": [{"weight": 40, "amount": 600}, {"weight": 60, "amount": 400}]},
                      {"total": float("nan"), "assets": good["assets"]}):
            self.assertFalse(run(value)["passed"])

    def test_dashboard_requires_exact_known_fixture_statistics(self):
        task = {"outputs": ["metrics.json"], "validator": "dashboard", "expected_metrics": {"hours": 3.0, "cancelled_count": 1}}
        self.assertTrue(assess_files(task, {"metrics.json": b'{"hours":3.0,"cancelled_count":1}'})["passed"])
        self.assertFalse(assess_files(task, {"metrics.json": b'{"hours":4.0,"cancelled_count":1}'})["passed"])

    def test_sources_require_delivered_body_and_dates(self):
        task = {"outputs": ["sources.json"], "validator": "sources"}
        rows = [{"url": "https://example.org/a", "published_at": "2026-11-01", "retrieved_at": "2026-11-18T00:00:00Z", "text_path": "sources/a.txt"},
                {"url": "https://example.org/b", "published_at": "2026-11-02", "retrieved_at": "2026-11-18T00:00:00Z", "text_path": "sources/b.txt"}]
        files = {"sources.json": json.dumps(rows).encode(), "sources/a.txt": b"body A", "sources/b.txt": b"body B"}
        result = assess_files(task, files)
        self.assertTrue(result["passed"])
        self.assertFalse(result["quality_verified"])
        rows[0]["published_at"] = "not a publication date"
        invalid = {**files, "sources.json": json.dumps(rows).encode()}
        self.assertFalse(assess_files(task, invalid)["passed"])
        del files["sources/b.txt"]
        self.assertFalse(assess_files(task, files)["passed"])


class TestpackIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("testpack-integration")
        self.service = StudioService(self.root / "runs", Settings(api_key="fixture-key"))

    def tearDown(self):
        self.service.close()
        cleanup(self.root)

    def pack(self):
        pack = load_pack(PACK)
        # Integration of orchestration is independent of content grading. Six
        # different outputs make stale cross-task success impossible.
        for scene in pack["scenarios"]:
            for number, task in enumerate(scene["tasks"], 1):
                task.update(outputs=[f"{scene['id']}-{number}.md"], validator=None, checks=[])
        return pack

    def script(self, scene, task, prior=None):
        workers = [p["id"] for p in scene["profiles"] if p["id"] != scene["lead"]]
        script = [tool("ask_human", question="Confirm this fixture run.", decision_kind="business_requirement")]
        for i, role in enumerate(workers):
            script += [tool("delegate_task", assignee=role, objective="Verify assigned fixture work", remaining_roles=workers[i+1:])]
            if i == 0:
                if prior:
                    script.append(tool("read_file", path=prior))
                script.append(tool("write_file", path=task["outputs"][0], content="Fixture output for " + task["id"]))
            else:
                script.append(tool("read_file", path=task["outputs"][0]))
            script.append({"text": "Verified the delivered fixture file."})
        return script + [{"text": "Four-role fixture complete."}]

    def test_six_runs_three_teams_continuation_and_complete_exports(self):
        pack = self.pack()
        providers = [ScriptedProvider(self.script(scene, task, scene["tasks"][0]["outputs"][0] if i else None))
                     for scene in pack["scenarios"] for i, task in enumerate(scene["tasks"])]
        with patch("miniagent.studio_service.DeepSeekProvider", side_effect=providers):
            report = run_pack(pack, self.service, self.root / "evidence", answer=lambda _: "fixture approved", poll_interval=.01)
        self.assertTrue(report["machine_passed"], report)
        self.assertFalse(report["formal_acceptance"])
        ids = []
        for scene in report["scenarios"]:
            self.assertEqual(len(scene["tasks"]), 2)
            first, second = scene["tasks"]
            ids.append(self.service.detail(first["run_id"])["conversation"]["id"])
            self.assertEqual(ids[-1], self.service.detail(second["run_id"])["conversation"]["id"])
            with zipfile.ZipFile(self.root / "evidence" / f"{scene['id']}-2.zip") as archive:
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(len(manifest), 2)
                self.assertTrue(manifest[0]["audit_files"])
                self.assertTrue(manifest[1]["human_timing"]["measurement_complete"])
                execution = json.loads(archive.read("turn-002/audit/execution.json"))
                self.assertTrue(execution["runtime"]["source_sha256"])
                trace = archive.read("turn-002/audit/trace.jsonl").decode()
                self.assertIn('"receipt_complete": true', trace)
                self.assertIn('"requested_arguments"', trace)
                self.assertNotIn("fixture-key", trace)
                memory = json.loads(archive.read("turn-002/audit/role-memory.json"))
                self.assertTrue(memory["enabled"])
                self.assertEqual(len(memory["roles"]), 4)
                self.assertTrue(all(len(rows) == 2 for rows in memory["roles"].values()), memory)
        self.assertEqual(len(set(ids)), 3)

    def test_task_one_failure_preserves_evidence_and_blocks_task_two(self):
        pack = self.pack()
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=ScriptedProvider([{"text": "I did not create any file."}])):
            report = run_pack(pack, self.service, self.root / "failure", scenario_id="software", answer=lambda _: None)
        self.assertFalse(report["machine_passed"])
        self.assertEqual(len(report["scenarios"][0]["tasks"]), 1)
        self.assertIn("blocked_followup", report["scenarios"][0])
        self.assertTrue((self.root / "failure/software-1.zip").exists())
        self.assertEqual(len(self.service.list_runs()["runs"]), 1)
        self.assertTrue((self.root / "failure/report.json").exists())


if __name__ == "__main__":
    unittest.main()
