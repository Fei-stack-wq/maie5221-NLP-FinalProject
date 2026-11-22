"""``run_browser``: run a Python Playwright script against a real headless Chromium."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from .schemas import ToolResult
from .shell_tool import INLINE_FAILURE_LIMIT, run_posix_argv


def _browser_cache() -> str:
    # The child gets a disposable HOME; point the driver at the normal browser
    # cache explicitly without exposing the user's profile or cookies.
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return "0" if configured == "0" else str(Path(configured).expanduser().resolve())
    if sys.platform == "darwin":
        return str(Path.home() / "Library" / "Caches" / "ms-playwright")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return str(cache / "ms-playwright")


def run_browser(ctx, args: dict) -> ToolResult:
    if os.name != "posix":
        return ToolResult.failure("run_browser needs macOS or Linux.")
    code = args["code"]
    if not code.strip() or len(code) > 20_000 or "\x00" in code:
        return ToolResult.failure("The browser script must be 1–20000 characters of Python.")
    cwd = ctx.sandbox.resolve(args.get("cwd", "."), must_exist=True)
    if not cwd.is_dir():
        return ToolResult.failure("cwd must be a directory in the workspace.")
    runner = Path(__file__).with_name("playwright_runner.py").resolve()
    with tempfile.TemporaryDirectory(prefix="miniagent-browser-", dir=ctx.sandbox.command_tmpdir) as home:
        argv = ["python", "-I", "-B", "-u", str(runner), _browser_cache(), home, str(cwd), code]
        return run_posix_argv(ctx, argv, cwd, args.get("timeout"), failure_evidence_limit=INLINE_FAILURE_LIMIT)
