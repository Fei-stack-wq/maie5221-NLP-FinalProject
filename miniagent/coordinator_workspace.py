"""A revision-scoped workbench for group coordinators' checks; never a publication source."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import os
import shutil
import sys
import tempfile

from .member_processes import managed_member_processes
from .sandbox import Sandbox
from .schemas import ToolResult
from .studio_conversations import copy_project


def isolated_command(argv, scratch, runtime, history_root):
    """Mount or allow only this disposable session for writes by the child and its descendants."""
    if sys.platform == "darwin":
        # Seatbelt parameters keep paths out of the profile's expression syntax.
        profile = ('(version 1)(allow default)(deny file-write*)'
                   '(allow file-write* (subpath (param "SCRATCH")) (subpath (param "RUNTIME"))'
                   ' (literal "/dev/null") (literal "/dev/tty"))'
                   '(deny file-read* (subpath (param "HISTORY")))')
        return ["/usr/bin/sandbox-exec", "-D", f"SCRATCH={scratch}", "-D", f"RUNTIME={runtime}",
                "-D", f"HISTORY={history_root}", "-p", profile, *argv]
    if sys.platform.startswith("linux") and (binary := shutil.which("bwrap", path=os.defpath)):
        return [binary, "--die-with-parent", "--unshare-pid", "--ro-bind", "/", "/", "--proc", "/proc",
                "--dev", "/dev", "--tmpfs", str(history_root), "--bind", str(scratch), str(scratch),
                "--bind", str(runtime), str(runtime), "--", *argv]
    raise OSError("协调检查需要 macOS 的 sandbox-exec 或 Linux 的 bubblewrap (bwrap)。")


class CoordinatorWorkspace:
    def __init__(self, crew):
        self.crew = crew
        self.resources = ExitStack()
        self.revision = None
        self.sandbox = None
        self.inspections = set()
        self.executions = {}
        self.private_changes_possible = False

    def close(self):
        self.resources.close()

    def _current(self):
        if self.revision == self.crew.project_version:
            return self.sandbox
        self.close()
        self.revision = self.sandbox = None
        self.inspections.clear()
        self.executions.clear()
        self.private_changes_possible = False
        with ExitStack() as opening:
            # Darwin Unix-domain sockets have a short absolute pathname limit.
            base = Path(opening.enter_context(tempfile.TemporaryDirectory(
                prefix="studio-check-", dir="/tmp" if sys.platform == "darwin" else None))).resolve()
            scratch, runtime = base / "project", base / "runtime"
            # Host populates only a newly created directory, never follows paths
            # that an earlier command could have replaced with a symlink.
            copy_project(self.crew.path, self.crew.lead, scratch, self.crew.extensions,
                         secret=self.crew.settings.api_key)
            runtime.mkdir()
            sandbox = Sandbox(scratch, agent_name=self.crew.lead)
            sandbox.command_tmpdir = runtime
            sandbox.command_wrapper = lambda argv: isolated_command(argv, scratch, runtime, self.crew.path.parent)
            opening.enter_context(managed_member_processes(sandbox))
            self.resources = opening.pop_all()
        self.revision, self.sandbox = self.crew.project_version, sandbox
        return sandbox

    def wrap(self, tool):
        def call(ctx, args):
            sandbox = self._current()
            if tool.name in {"run_python", "run_shell", "run_browser"} and os.name != "posix":
                return ToolResult.failure("此平台不支持隔离的协调检查进程。")
            if tool.name in {"run_python", "run_shell", "run_browser", "search_sources", "http_get"}:
                self.private_changes_possible = True
            result = tool.fn(replace(ctx, sandbox=sandbox), args)
            result.meta["coordinator_private_copy"] = self.private_changes_possible
            if tool.name in {"run_python", "run_shell", "run_browser", "poll_process"}:
                exit_code = result.meta.get("exit_code")
                if type(exit_code) is int:
                    self.executions[tool.name] = {"exit_code": exit_code}
                    if hashes := result.meta.get("asserted_browser_files"):
                        self.executions[tool.name]["asserted_files"] = dict(list(hashes.items())[:8])
            elif result.ok:
                self.inspections.add(tool.name)
            return result
        return replace(tool, fn=call)
