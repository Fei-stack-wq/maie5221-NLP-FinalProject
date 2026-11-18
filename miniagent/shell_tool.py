"""Shared POSIX command runner for Python and noninteractive project shells."""
from __future__ import annotations

import math
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
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


def _drain(process, stdout, stderr, deadline, should_cancel):
    with selectors.DefaultSelector() as poller:
        for stream, sink in [(process.stdout, stdout), (process.stderr, stderr)]:
            poller.register(stream, selectors.EVENT_READ, sink)
        exited_at = None
        while poller.get_map() or process.poll() is None:
            now = time.monotonic()
            if should_cancel and should_cancel():
                return False
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
            if not poller.get_map():
                time.sleep(wait)
                continue
            events = poller.select(wait)
            if not events and exited_at is not None:
                break
            for key, _ in events:
                chunk = os.read(key.fd, 16384)
                if chunk:
                    key.data.add(chunk)
                else:
                    poller.unregister(key.fileobj)
    return True


def run_shell(ctx: ToolContext, args: dict) -> ToolResult:
    if os.name != "posix":
        return ToolResult.failure("run_shell 需要本机 POSIX shell。")
    command = args["command"]
    if not command.strip() or len(command) > 20000 or "\x00" in command:
        return ToolResult.failure("命令须为 1 至 20000 字符的非空文本。")
    cwd = ctx.sandbox.resolve(args.get("cwd", "."), must_exist=True)
    if not cwd.is_dir():
        return ToolResult.failure("命令工作目录必须是项目中的目录。")
    argv = ["/bin/sh", "-c", command]
    if "yield_after_seconds" in args:
        sessions = ctx.sandbox.command_sessions
        if sessions is None:
            return ToolResult.failure("此工作区没有活动进程作用域，请省略 yield_after_seconds 以前台执行。")
        return sessions.start(ctx, argv, cwd, args)
    return run_posix_argv(ctx, argv, cwd, args.get("timeout"))


def command_timeout(ctx, requested):
    seconds = 120.0 if requested is None else float(requested)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("超时秒数必须是有限正数。")
    timeout = min(seconds, ctx.limits.tool_timeout_seconds)
    if ctx.deadline is not None:
        timeout = min(timeout, max(0, ctx.deadline - time.monotonic()))
    return timeout


def run_posix_argv(ctx: ToolContext, argv: list[str], cwd: Path, requested_timeout=None) -> ToolResult:
    try:
        timeout = command_timeout(ctx, requested_timeout)
    except ValueError as exc:
        return ToolResult.failure(str(exc))
    if timeout <= 0 or ctx.should_cancel and ctx.should_cancel():
        return ToolResult.failure("时间已耗尽或已取消，未启动命令。")
    started = time.monotonic()
    stdout, stderr = _Output(), _Output()
    timed_out = False
    cancelled = False
    with subprocess.Popen(
        argv, cwd=cwd, env=project_environment(ctx), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, start_new_session=True,
    ) as process:
        try:
            cancelled = not _drain(process, stdout, stderr, started + timeout, ctx.should_cancel)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if timed_out or cancelled:
                _stop_timed_out_process(process)
                process.wait()
            else:
                if process.poll() is None or ctx.sandbox.track_shell_group is None:
                    # Only managed members can own a background server between calls.
                    _kill_group(process.pid)
                else:
                    ctx.sandbox.track_shell_group(process.pid)
    elapsed = round((time.monotonic() - started) * 1000)
    parts = [f"exit_code={process.returncode}"]
    if timed_out:
        parts.append(f"命令执行超时（{timeout:g} 秒），已停止。")
    if cancelled:
        parts.append("命令已取消，已回收进程。")
    for name, stream in [("stdout", stdout), ("stderr", stderr)]:
        if stream.count:
            parts.append(f"{name}:\n{stream.text()}")
    if not stdout.count and not stderr.count and not timed_out:
        parts.append("（无输出）")
    return ToolResult(
        ok=not timed_out and not cancelled and process.returncode == 0, content="\n".join(parts),
        meta={"exit_code": process.returncode, "elapsed_ms": elapsed, "timeout_s": timeout,
              "timed_out": timed_out, "cancelled": cancelled, "cwd": ctx.sandbox.relative(cwd), "stdout_bytes": stdout.count,
              "stderr_bytes": stderr.count, "truncated": stdout.truncated or stderr.truncated},
    )


class CommandSessions:
    """Uncollected shell results belong to one member scope, including after exit."""

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="miniagent-shell")
        self.stopping = threading.Event()
        self.jobs = {}

    def start(self, ctx, argv, cwd, args):
        wait = args["yield_after_seconds"]
        if not math.isfinite(wait) or wait < 0:
            return ToolResult.failure("等待时间须为非负有限秒数。")
        if len(self.jobs) >= 4:
            return ToolResult.failure("当前成员已有四个未领取的进程结果，请先用 poll_process 领取。")
        # Validate before issuing a handle, even when the caller asks for an immediate yield.
        timeout = command_timeout(ctx, args.get("timeout"))
        if timeout <= 0:
            return ToolResult.failure("本次执行时间已用完。")
        handle = uuid.uuid4().hex
        def cancelled():
            return self.stopping.is_set() or bool(ctx.should_cancel and ctx.should_cancel())
        worker = replace(ctx, should_cancel=cancelled)
        future = self.executor.submit(run_posix_argv, worker, argv, cwd, timeout)
        self.jobs[handle] = (future, args["command"])
        return self.poll(handle, min(wait, timeout))

    def poll(self, handle, wait):
        job = self.jobs.get(handle)
        if job is None:
            return ToolResult.failure("当前分工没有该进程，或它的最终结果已领取。")
        if not math.isfinite(wait) or wait < 0:
            return ToolResult.failure("等待时间须为非负有限秒数。")
        future, command = job
        try:
            result = future.result(timeout=wait)
        except FutureTimeout:
            # A completion racing this timeout remains available on the next poll.
            return ToolResult.success(f"运行中：{handle}。用 poll_process 领取最终输出和退出码。",
                                      process_id=handle, process_state="running")
        except Exception:
            del self.jobs[handle]
            raise
        del self.jobs[handle]
        state = "exited" if "exit_code" in result.meta else "not_started"
        if result.meta.get("cancelled"):
            state = "cancelled"
        elif result.meta.get("timed_out"):
            state = "timed_out"
        result.meta.update(process_id=handle, process_state=state, command=command)
        return result

    def close(self):
        self.stopping.set()
        self.executor.shutdown(wait=True)
        self.jobs.clear()

    def pending_reason(self):
        if self.jobs:
            return "命令尚未领取最终结果。请用 poll_process 查询这些句柄：" + "、".join(self.jobs)
        return ""


def poll_process(ctx, args):
    sessions = ctx.sandbox.command_sessions
    if sessions is None:
        return ToolResult.failure("此工作区没有活动进程作用域。")
    wait = args.get("wait_seconds", 10.0)
    if not math.isfinite(wait) or wait < 0:
        return ToolResult.failure("等待时间须为非负有限秒数。")
    wait = min(wait, ctx.limits.tool_timeout_seconds)
    if ctx.deadline is not None:
        wait = min(wait, max(0, ctx.deadline - time.monotonic()))
    return sessions.poll(args["process_id"], wait)
