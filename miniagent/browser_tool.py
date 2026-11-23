"""``run_browser``: run a Python Playwright script against a real headless Chromium."""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

from .schemas import ToolResult
from .shell_tool import INLINE_FAILURE_LIMIT, command_timeout, project_environment, run_posix_argv

RUNNER = Path(__file__).with_name("playwright_runner.py").resolve()
SHOWN = 12_000


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


def _shown(text: str) -> str:
    if len(text) <= SHOWN:
        return text
    half = SHOWN // 2
    return text[:half] + f"\n…[{len(text) - SHOWN} characters omitted]…\n" + text[-half:]


class BrowserSession:
    """One warm Chromium for one agent run, reused by every ``run_browser`` call in that run.

    Starting Python, the Playwright driver and Chromium for each check took about
    14 seconds a call in the Mid_Test runs. Playwright MCP keeps one browser per
    session and closes it when the session ends; this does the same per agent run,
    under the same sandbox wrapper, environment, timeout and cancellation as a
    shell command. A crash or timeout stops the server; the next call starts a
    new one. Each call still opens a fresh browser context.
    """

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.replies: queue.Queue = queue.Queue()
        self.stderr: deque[str] = deque(maxlen=80)
        self.lock = threading.Lock()
        self.calls = 0

    def _start(self, ctx) -> str | None:
        home = tempfile.mkdtemp(prefix="miniagent-browser-", dir=ctx.sandbox.command_tmpdir)
        argv = ["python", "-I", "-B", "-u", str(RUNNER), "--serve", _browser_cache(), home]
        if ctx.sandbox.command_wrapper is not None:
            argv = ctx.sandbox.command_wrapper(argv)
        root = ctx.sandbox.resolve(".", must_exist=True)
        self.replies = queue.Queue()
        self.process = subprocess.Popen(argv, cwd=root, env=project_environment(ctx), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                        text=True, encoding="utf-8", errors="replace", bufsize=1)
        if ctx.sandbox.track_shell_group is not None:
            ctx.sandbox.track_shell_group(self.process.pid)  # killed with the run even if close() is skipped
        process, replies, stderr = self.process, self.replies, self.stderr

        def read_replies():
            try:
                for line in process.stdout:
                    replies.put(line)
            except (OSError, ValueError):
                pass
            replies.put(None)

        def read_stderr():
            try:
                for line in process.stderr:
                    stderr.append(line)
            except (OSError, ValueError):
                pass

        threading.Thread(target=read_replies, daemon=True, name="browser-replies").start()
        threading.Thread(target=read_stderr, daemon=True, name="browser-stderr").start()
        reply = self._wait(ctx, 90)
        if reply is None or not reply.get("ready"):
            self.close()
            return "".join(self.stderr)[-4000:] or "the browser server did not start"
        return None

    def _wait(self, ctx, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while True:
            if ctx.should_cancel and ctx.should_cancel():
                raise InterruptedError("cancelled")
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            try:
                line = self.replies.get(timeout=min(0.5, left))
            except queue.Empty:
                continue
            return None if line is None else json.loads(line)

    def run(self, ctx, cwd: Path, code: str, requested_timeout) -> ToolResult:
        with self.lock:
            try:
                timeout = command_timeout(ctx, requested_timeout)
            except ValueError as exc:
                return ToolResult.failure(str(exc))
            started = time.monotonic()
            if self.process is None or self.process.poll() is not None:
                problem = self._start(ctx)
                if problem:
                    return ToolResult.failure("The browser could not start:\n" + problem)
            self.calls += 1
            try:
                self.process.stdin.write(json.dumps({"cwd": str(cwd), "workspace": str(cwd), "code": code}) + "\n")
                self.process.stdin.flush()
                reply = self._wait(ctx, timeout)
            except (TimeoutError, InterruptedError, BrokenPipeError, OSError) as exc:
                self.close()
                status = "Cancelled" if isinstance(exc, InterruptedError) else f"Timed out ({timeout:g}s)"
                if isinstance(exc, (BrokenPipeError, OSError)) and not isinstance(exc, (TimeoutError, InterruptedError)):
                    status = "The browser server stopped"
                return ToolResult.failure(f"exit_code=None\n{status}; the browser was closed and the next "
                                          "run_browser call starts a new one.",
                                          timed_out=isinstance(exc, TimeoutError), warm_browser=True)
            if reply is None:
                self.close()
                return ToolResult.failure("exit_code=None\nThe browser server stopped:\n"
                                          + "".join(self.stderr)[-4000:])
            parts = [f"exit_code={reply['status']}"]
            for name in ("stdout", "stderr"):
                if reply[name]:
                    parts.append(f"{name}:\n{_shown(reply[name])}")
            if not reply["stdout"] and not reply["stderr"]:
                parts.append("(no output)")
            return ToolResult(ok=reply["status"] == 0, content="\n".join(parts),
                              meta={"exit_code": reply["status"], "elapsed_ms": round((time.monotonic() - started) * 1000),
                                    "timeout_s": timeout, "cwd": ctx.sandbox.relative(cwd), "warm_browser": True})

    def close(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def run_browser(ctx, args: dict) -> ToolResult:
    if os.name != "posix":
        return ToolResult.failure("run_browser needs macOS or Linux.")
    code = args["code"]
    if not code.strip() or len(code) > 20_000 or "\x00" in code:
        return ToolResult.failure("The browser script must be 1–20000 characters of Python.")
    cwd = ctx.sandbox.resolve(args.get("cwd", "."), must_exist=True)
    if not cwd.is_dir():
        return ToolResult.failure("cwd must be a directory in the workspace.")
    session = getattr(ctx.sandbox, "browser_session", None)
    if session is not None:
        return session.run(ctx, cwd, code, args.get("timeout"))
    with tempfile.TemporaryDirectory(prefix="miniagent-browser-", dir=ctx.sandbox.command_tmpdir) as home:
        argv = ["python", "-I", "-B", "-u", str(RUNNER), _browser_cache(), home, str(cwd), code]
        return run_posix_argv(ctx, argv, cwd, args.get("timeout"), failure_evidence_limit=INLINE_FAILURE_LIMIT)
