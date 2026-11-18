"""Shared POSIX command runner for Python and noninteractive project shells."""
from __future__ import annotations

import math
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

from .schemas import ToolResult

if TYPE_CHECKING:
    from .tools import ToolContext


class _Output:
    """Keep both the initial diagnostic and final test summary of each stream."""

    head_limit = 1400
    tail_limit = 2200

    def __init__(self):
        self.head = bytearray()
        self.tail = bytearray()
        self.count = 0

    def add(self, chunk):
        self.count += len(chunk)
        take = min(self.head_limit - len(self.head), len(chunk))
        self.head.extend(chunk[:take])
        self.tail.extend(chunk[take:])
        if len(self.tail) > self.tail_limit:
            del self.tail[:-self.tail_limit]

    @property
    def truncated(self):
        return self.count > self.head_limit + self.tail_limit

    def text(self):
        marker = "\n…[已截断]…\n".encode() if self.truncated else b""
        return (bytes(self.head) + marker + self.tail).decode("utf-8", errors="replace")


def project_environment(ctx):
    # No inherited login shell, cloud tokens, proxy credentials, SSH agent,
    # or user startup files. The Studio scope lets it release local test
    # servers started by this member when it hands off.
    env = {key: os.environ[key] for key in ("LANG", "LC_ALL", "LC_CTYPE", "SystemRoot", "WINDIR",
                                            "PATHEXT", "TMP", "TEMP", "TMPDIR") if key in os.environ}
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", os.defpath)])
    if sys.prefix != sys.base_prefix:
        env["VIRTUAL_ENV"] = sys.prefix
    env.update(HOME=str(ctx.sandbox.root), CI="true", GIT_TERMINAL_PROMPT="0", PAGER="cat", NO_COLOR="1",
               NO_PROXY="localhost,127.0.0.1,::1")
    if os.name == "nt":
        env["USERPROFILE"] = str(ctx.sandbox.root)
    marker = "MINIAGENT_MEMBER_PROCESS_SCOPE"
    if marker in ctx.sandbox.process_env:
        env[marker] = ctx.sandbox.process_env[marker]
    return env


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _stop_timed_out_process(process):
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except (OSError, psutil.Error):
        children = []
    _kill_group(process.pid)
    for child in children:
        try:
            child.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    # SIGKILL delivery is asynchronous. Waiting for the launcher alone can
    # return while a browser descendant is still running its exit path.
    # Observe those exact process identities, with a bounded cleanup grace.
    cleanup_deadline = time.monotonic() + 1.0
    pending = children
    while pending and time.monotonic() < cleanup_deadline:
        running = []
        for child in pending:
            try:
                if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                    running.append(child)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        pending = running
        if pending:
            time.sleep(0.01)


def _drain(process, stdout, stderr, deadline):
    with selectors.DefaultSelector() as poller:
        for stream, sink in [(process.stdout, stdout), (process.stderr, stderr)]:
            poller.register(stream, selectors.EVENT_READ, sink)
        exited_at = None
        while poller.get_map():
            now = time.monotonic()
            if process.poll() is not None:
                # An intentional background service may retain a pipe after
                # its launching shell exits. Only drain already pending output.
                if exited_at is None:
                    exited_at = now
                if now - exited_at > 0.1:
                    break
            if now >= deadline and exited_at is None:
                raise subprocess.TimeoutExpired(process.args, 0)
            wait = 0 if exited_at is not None else min(0.1, max(0, deadline - now))
            events = poller.select(wait)
            if not events and exited_at is not None:
                break
            for key, _ in events:
                chunk = os.read(key.fd, 16384)
                if chunk:
                    key.data.add(chunk)
                else:
                    poller.unregister(key.fileobj)
    process.wait(timeout=max(0, deadline - time.monotonic()))


def run_shell(ctx: ToolContext, args: dict) -> ToolResult:
    if os.name != "posix":
        return ToolResult.failure("run_shell 需要本机 POSIX shell。")
    command = args["command"]
    if not command.strip() or len(command) > 20000 or "\x00" in command:
        return ToolResult.failure("命令须为 1 至 20000 字符的非空文本。")
    cwd = ctx.sandbox.resolve(args.get("cwd", "."), must_exist=True)
    if not cwd.is_dir():
        return ToolResult.failure("命令工作目录必须是项目中的目录。")
    return run_posix_argv(ctx, ["/bin/sh", "-c", command], cwd, args.get("timeout"))


def run_posix_argv(ctx: ToolContext, argv: list[str], cwd: Path, requested_timeout=None) -> ToolResult:
    limit = ctx.limits.tool_timeout_seconds
    timeout = min(float(requested_timeout) if requested_timeout is not None else limit, limit)
    if not math.isfinite(timeout) or timeout <= 0:
        return ToolResult.failure("超时秒数必须是正数。")
    started = time.monotonic()
    stdout, stderr = _Output(), _Output()
    timed_out = False
    with subprocess.Popen(
        argv, cwd=cwd, env=project_environment(ctx), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, start_new_session=True,
    ) as process:
        try:
            _drain(process, stdout, stderr, started + timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop_timed_out_process(process)
            process.wait()
        finally:
            if not timed_out:
                if process.poll() is None or ctx.sandbox.track_shell_group is None:
                    # Only managed members can own a background server between calls.
                    _kill_group(process.pid)
                else:
                    ctx.sandbox.track_shell_group(process.pid)
    elapsed = round((time.monotonic() - started) * 1000)
    parts = [f"exit_code={process.returncode}"]
    if timed_out:
        parts.append(f"命令执行超时（{timeout:g} 秒），已停止。")
    for name, stream in [("stdout", stdout), ("stderr", stderr)]:
        if stream.count:
            parts.append(f"{name}:\n{stream.text()}")
    if not stdout.count and not stderr.count and not timed_out:
        parts.append("（无输出）")
    return ToolResult(
        ok=not timed_out and process.returncode == 0, content="\n".join(parts),
        meta={"exit_code": process.returncode, "elapsed_ms": elapsed, "timeout_s": timeout,
              "timed_out": timed_out, "cwd": ctx.sandbox.relative(cwd), "stdout_bytes": stdout.count,
              "stderr_bytes": stderr.count, "truncated": stdout.truncated or stderr.truncated},
    )
