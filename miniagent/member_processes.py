"""Let a Studio member keep a local test server between tools, then release it."""
from __future__ import annotations

import uuid
import os
import signal
from contextlib import contextmanager
from pathlib import Path

import psutil

from .shell_tool import CommandSessions

_MARKER = "MINIAGENT_MEMBER_PROCESS_SCOPE"


@contextmanager
def managed_member_processes(sandbox):
    token = uuid.uuid4().hex
    sandbox.process_env = {_MARKER: token}
    receipt = {"stopped": 0, "status": "finished"}
    groups = {}

    def track_group(pgid):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        # We launched this shell in its own session. Record real survivors
        # before returning to the caller, including protected OS binaries for
        # which macOS will not expose environ(). Process identity includes
        # creation time, protecting a later cleanup from PID reuse.
        survivors = []
        try:
            for process in psutil.process_iter():
                try:
                    if os.getpgid(process.pid) == pgid:
                        survivors.append(process)
                except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
                    continue
        except (OSError, psutil.Error):
            # If survivors cannot be identified, close the still-owned group
            # now instead of leaving a background command without an owner.
            _signal_group(pgid, signal.SIGKILL)
            receipt["status"] = "unavailable"
            return
        if survivors:
            groups[pgid] = survivors

    sandbox.track_shell_group = track_group
    commands = CommandSessions()
    sandbox.command_sessions = commands
    try:
        yield receipt
    finally:
        if commands.jobs:
            receipt["uncollected_commands"] = len(commands.jobs)
        commands.close()
        sandbox.command_sessions = None
        sandbox.process_env = {}
        sandbox.track_shell_group = None
        try:
            for pgid, members in groups.items():
                live = []
                for process in members:
                    try:
                        if process.is_running() and os.getpgid(process.pid) == pgid:
                            live.append(process)
                    except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
                        continue
                if live:
                    _signal_group(pgid, signal.SIGKILL)
                    stopped, _ = psutil.wait_procs(live, timeout=1)
                    receipt["stopped"] += len(stopped)
            receipt["stopped"] += _release(sandbox.root, token)
        except (OSError, psutil.Error) as exc:
            # Restricted hosts may deny process inventory. Record the cleanup
            # outcome independently of the member's work and its acceptance.
            receipt.update(status="unavailable", reason=type(exc).__name__)


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _release(root, token):
    owned = []
    for process in psutil.process_iter(["cwd"]):
        cwd = process.info["cwd"]
        if not cwd or not Path(cwd).is_relative_to(root):
            continue
        try:
            if process.environ().get(_MARKER) == token:
                process.terminate()
                owned.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, remaining = psutil.wait_procs(owned, timeout=1)
    for process in remaining:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, remaining = psutil.wait_procs(remaining, timeout=1)
    return len(owned) - len(remaining)
