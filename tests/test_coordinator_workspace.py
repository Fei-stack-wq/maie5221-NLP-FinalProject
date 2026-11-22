"""Exercise real coordinator tools through the group-task entrypoint."""
import json
import re
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioService
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster
from miniagent.coordinator_workspace import CoordinatorWorkspace, isolated_command
from miniagent.tools import ToolContext, build_default_registry
from miniagent.sandbox import Sandbox
from miniagent.research import Page
from tests.test_studio_service import finished, request


requires_local_runner = unittest.skipUnless(
    sys.platform == "darwin" or (sys.platform.startswith("linux") and shutil.which("bwrap", path=os.defpath)),
    "System isolation integration requires macOS sandbox-exec or Linux bubblewrap with namespaces enabled.",
)


class CoordinatorWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.service = StudioService(self.root / "runs", Settings(api_key="unit"))
        self.addCleanup(self.service.close)

    def run_group(self, script, **options):
        provider = RecordingProvider(script)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            job = self.service.start(request("team", workspace_path=str(self.project),
                objective="核实当前公开页面，报告结果。", conversation=roster("lead", "dev"), **options))
            done = finished(self.service, job["id"])
        return done, provider

    @requires_local_runner
    def test_lead_verifies_in_own_copy_with_a_real_browser_and_protects_original(self):
        original = self.project / "index.html"
        original.write_text("<h1>Approved</h1>")
        script = [tool("run_python", code="from pathlib import Path\n"
                      "Path('index.html').write_text('<h1>Private check</h1>')\n"
                      "Path('check.txt').write_text('temporary assertion')"),
                  tool("read_file", path="check.txt"),
                  tool("run_shell", command="cat check.txt"),
                  tool("run_browser", code="from pathlib import Path\n"
                       "page.goto(Path('index.html').resolve().as_uri())\n"
                       "assert page.locator('h1').inner_text() == 'Private check'"),
                  tool("run_python", code=f"from pathlib import Path\nPath({str(original)!r}).write_text('bad')"),
                  {"text":"已用 Chromium 复核，临时测试保存在检查副本中。"}]
        detail, provider = self.run_group(script)
        trace = [json.loads(row) for row in (self.service.runs_dir / detail["run"]["id"] / "trace.jsonl").read_text().splitlines()]
        results = [(e["tool"], e["ok"], e.get("preview")) for e in trace if e["kind"] == "tool_result"]
        self.assertEqual([(n, ok) for n, ok, _ in results], [("run_python", True), ("read_file", True),
            ("run_shell", True), ("run_browser", True), ("run_python", False)], results)
        self.assertIn("temporary assertion", results[1][2])
        self.assertTrue({"search_sources","http_get","read_source","run_browser","run_shell","poll_process"} <= set(provider.requests[0][1]))
        self.assertNotIn("submit_review", provider.requests[0][1])
        self.assertNotIn("write_file", provider.requests[0][1])
        self.assertEqual(detail["run"]["status"], "unverified", detail["diagnostics"])
        self.assertFalse(any(e["kind"]=="assignment" for e in detail["room_events"]))
        self.assertEqual(original.read_text(), "<h1>Approved</h1>")
        self.assertFalse((self.project/"check.txt").exists())
        archived = self.service.runs_dir / detail["run"]["id"] / "agents/lead"
        self.assertEqual((archived/"index.html").read_text(), original.read_text())
        self.assertFalse((archived/"check.txt").exists())


    @requires_local_runner
    def test_revisions_refresh_contents_and_never_publish_scratch_or_remote_sources(self):
        opts = Settings(api_key="fixture")
        public = self.root / "runs" / "r"
        source = public / "agents" / "lead"
        source.mkdir(parents=True)
        page = source / "index.html"
        page.write_text("first")
        crew = SimpleNamespace(path=public, project_version=1, lead="lead", extensions={".html"}, settings=opts)
        bench = CoordinatorWorkspace(crew)
        self.addCleanup(bench.close)
        defaults = build_default_registry()
        ctx = ToolContext(Sandbox(source), opts.limits)

        def check(name, **args):
            result = bench.wrap(defaults.get(name)).fn(ctx, args)
            self.assertTrue(result.ok, result.content)
            return result

        with patch("miniagent.research._service", return_value=SimpleNamespace(body=b'{"message":{"items":[]}}', truncated=False)):
            search = check("search_sources", query="original sources", collection="papers")
        path, = set(re.findall(r"sources/[a-f0-9]{64}\.json", search.content))
        self.assertIn("original sources", check("read_file", path=path).content)
        self.assertFalse((source / path).exists())
        url = "https://example.org/source"
        with patch("miniagent.research.fetch", return_value=Page(url, "text/plain", b"Traceable source text", False)):
            body = check("http_get", url=url)
        archived, = set(re.findall(r"sources/[a-f0-9]{64}\.json", body.content))
        self.assertIn("Traceable source text", check("read_source", path=archived).content)
        self.assertFalse((source / archived).exists())

        shell = check("run_shell", command="printf ready", yield_after_seconds=0)
        if shell.meta.get("process_state") == "running":
            shell = check("poll_process", process_id=shell.meta["process_id"], wait_seconds=5)
        self.assertEqual(shell.meta["exit_code"], 0)
        self.assertIn("ready", shell.content)
        self.assertIn("first", check("read_file", path="index.html").content)
        check("run_python", code="from pathlib import Path; Path('index.html').write_text('scratch')")
        old = bench.sandbox.root
        page.write_text("second")
        crew.project_version += 1
        self.assertIn("second", check("read_file", path="index.html").content)
        self.assertFalse(old.exists())
        self.assertEqual(page.read_text(), "second")


    def test_inventory_is_evidence_for_an_answer_and_cannot_satisfy_a_requested_check(self):
        detail, requests = self.run_group([tool("list_dir", path="."), {"text":"目录为空。"}])
        self.assertEqual(detail["run"]["status"], "unverified")
        self.assertIn('"inspection_tools":["list_dir"],"executions":{}', str(requests.requests[-1][0]))
        report, = detail["acceptance"]
        self.assertEqual(report["report"]["verdict"], "unverified")
        self.assertEqual(report["receipts"], [])
        candidate = "approved.txt"
        script = [tool("list_dir", path="."), {"text":"已经通过。"},
                  tool("delegate_task", assignee="dev", objective="Produce the requested document",
                       remaining_roles=[], deliverable_paths=[candidate]),
                  tool("write_file", path=candidate, content="Approved document"),
                  {"text":"Approved document ready."}, {"text":"已交付。"}]
        checked, provider = self.run_group(script, acceptance_files=[candidate])
        self.assertEqual(checked["run"]["status"], "completed", checked["diagnostics"])
        self.assertIn("本次群任务还没有成员完成交接", str(provider.requests[2][0]))
        self.assertEqual([e["recipient"] for e in checked["room_events"] if e["kind"]=="assignment"], ["dev"])


    def test_missing_linux_backend_reports_an_actionable_error_without_launching_the_command(self):
        with patch("miniagent.coordinator_workspace.sys.platform", "linux"), patch(
                "miniagent.coordinator_workspace.shutil.which", return_value=None):
            with self.assertRaisesRegex(OSError, "bubblewrap"):
                isolated_command(["true"], self.root, self.root, self.service.runs_dir)
        with patch("miniagent.coordinator_workspace.sys.platform", "linux"), patch(
                "miniagent.coordinator_workspace.shutil.which", return_value="/usr/bin/bwrap"):
            command = isolated_command(["true"], self.root, self.project, self.service.runs_dir)
        self.assertEqual(command[command.index("--ro-bind"):][:3], ["--ro-bind", "/", "/"])
        self.assertIn("--unshare-pid", command)
        self.assertEqual(command[-2:], ["--", "true"])
