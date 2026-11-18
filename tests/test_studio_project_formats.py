"""Dependency installation can precede a verified handoff of real sources."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_conversations import SnapshotError, copy_project
from miniagent.studio_service import StudioError, StudioService, _EXTENSIONS
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import assign, roster
from tests.test_studio_service import finished, request


class StudioProjectFormatsTest(unittest.TestCase):
    def test_dependency_tree_excluded_while_sources_and_built_assets_reach_local_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            local = root / "local"
            local.mkdir()
            sources = ["main.go", "go.mod", "go.sum", "Cargo.lock", "main.c", "lib.h", "app.cpp", "lib.hpp",
                       "lib.rs", "App.java", "Makefile", "Dockerfile", "dist/app.js"]
            # npm-like real directory containing package JavaScript, an
            # executable link and an unsupported native binary. None is work.
            script = ("from pathlib import Path\n"
                      f"for name in {sources!r}:\n"
                      " p=Path(name); p.parent.mkdir(parents=True, exist_ok=True); p.write_text('delivered compiled project source')\n"
                      "pkg=Path('web/node_modules/pkg'); pkg.mkdir(parents=True); (pkg/'index.js').write_text('dep')\n"
                      "(pkg/'addon.node').write_bytes(bytes([0,255])); (pkg/'entry.js').symlink_to('index.js')\n"
                      "v=Path('venv/lib'); v.mkdir(parents=True); (v/'generated.py').write_text('dependency')\n")
            svc = StudioService(root / "runs", Settings(api_key="test-sentinel"))
            try:
                provider = RecordingProvider([assign("dev","implement and check build"),
                    tool("run_python",code=script), {"text":"source and built assets delivered"}, {"text":"ready"}])
                with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
                    run = svc.start(request("team", objective="deliver development project",
                                    conversation=roster("lead","dev"), workspace_path=str(local),
                                    acceptance_files=["main.go"]))
                    detail = finished(svc,run["id"])
                self.assertEqual(detail["run"]["status"], "completed", detail["run"])
                self.assertEqual({str(x.relative_to(local)) for x in local.rglob("*") if x.is_file()}, set(sources))
                report = next(e for e in detail["room_events"] if e["kind"] == "member_report")["handoff"]
                self.assertEqual(set(report["evidence"]["files"]), set(sources))
                self.assertNotIn("node_modules", json.dumps(detail["artifacts"]))
                self.assertEqual(svc.download_artifact(run["id"],"agents/lead/Makefile"),b"delivered compiled project source")
                with self.assertRaises(StudioError):
                    svc.download_artifact(run["id"],"agents/lead/web/node_modules/pkg/index.js")
            finally:
                svc.close()

    def test_skip_only_real_cache_directories_and_keep_sidecar_and_unknown_guards(self):
        for bad in ("link named node_modules", "unknown.bin", "live.db-wal"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                project = root / "agents" / "dev"
                project.mkdir(parents=True)
                if bad.startswith("link"):
                    (project / "node_modules").symlink_to(root, target_is_directory=True)
                else:
                    (project / bad).write_bytes(b"data")
                with self.assertRaises(SnapshotError):
                    copy_project(root,"dev",root/"staged",_EXTENSIONS)

    def test_progress_counts_only_project_changes_when_installing_dependencies(self):
        from miniagent.crew_progress import MemberProgress
        from miniagent.rao import Observation, RAOState, StepRecord
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            progress = MemberProgress(root)
            for p in ("node_modules/pkg/result.js","packages/a/venv/lib/data.py","web/__pycache__/a.pyc"):
                target=root/p
                target.parent.mkdir(parents=True)
                target.write_bytes(b"dependency")
            note = Observation(step_id="s",source="run_shell",ok=True,content="exit_code=0")
            progress.observe(StepRecord(step=1,observations=[note]),RAOState(goal="install",task_id="t"))
            self.assertEqual(progress.evidence()["files"],[])
