"""Opt-in publication to a host-designated independent Git test repository.

Prepare an immutable text snapshot and commit before asking for approval. Only
new nlp-test/* branches are supported. No shell interpolation, existing-branch
updates, deletion, remote override by the model, or implicit approval.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

from .schemas import ToolResult


def validate_remote(remote):
    if not isinstance(remote, str) or not remote or any(ord(c) < 32 for c in remote):
        raise ValueError("Git test remote is not configured")
    url = urlsplit(remote)
    if (url.scheme == "https" and url.hostname == "github.com" and not url.username and not url.password
            and not url.query and not url.fragment and not url.port
            and re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", url.path)):
        return remote
    if Path(remote).is_absolute() and Path(remote).is_dir():
        return str(Path(remote).resolve())
    raise ValueError("Git test remote must be an explicit GitHub HTTPS repository or a local bare test repository")


def publish(ctx, args, remote, *, secret=""):
    remote = validate_remote(remote)
    branch = args["branch"]
    if not isinstance(branch, str) or not re.fullmatch(r"nlp-test/[A-Za-z0-9][A-Za-z0-9_-]{0,63}", branch):
        raise ValueError("Only a new nlp-test/<name> branch is permitted")
    paths = args["files"]
    if not isinstance(paths, list) or not 1 <= len(paths) <= 64 or len(set(paths)) != len(paths):
        raise ValueError("Select 1..64 distinct text files")
    snapshots, hashes, total = {}, {}, 0
    for name in paths:
        if (not isinstance(name, str) or "\\" in name or ":" in name or PurePosixPath(name).is_absolute()
                or any(p in {"", ".", ".."} or p.startswith(".") for p in name.split("/"))
                or Path(name).suffix.lower() not in {".py", ".md", ".txt", ".json", ".csv", ".html", ".css", ".js", ".toml", ".yaml", ".yml"}):
            raise ValueError("Only ordinary relative project text files may be published")
        source = ctx.sandbox.resolve(name, must_exist=True)
        if not source.is_file() or source.is_symlink() or source.stat().st_size > 1024 * 1024:
            raise ValueError("Publication requires bounded regular files")
        data = source.read_bytes()
        data.decode("utf-8")
        from .project_data import validate_project_data
        validate_project_data(name, data, secret=secret, reject_credentials=True)
        total += len(data)
        if total > 4 * 1024 * 1024:
            raise ValueError("Publication exceeds 4 MiB")
        snapshots[name], hashes[name] = data, hashlib.sha256(data).hexdigest()
    executable = shutil.which("git")
    if not executable:
        return ToolResult.failure("Git is not installed")
    deadline = time.monotonic() + ctx.limits.tool_timeout_seconds
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    with tempfile.TemporaryDirectory(prefix="nlp-git-delivery-") as temporary:
        stage = Path(temporary)
        hooks = stage / "empty-hooks"
        hooks.mkdir()
        def git(*arguments, accepted=(0,)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Git publication deadline exhausted")
            command = [executable, "-c", "core.hooksPath=" + str(hooks),
                       "-c", "credential.interactive=false", *arguments]
            with subprocess.Popen(command, cwd=stage, env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name == "posix") as process:
                try:
                    stdout, _ = process.communicate(timeout=remaining)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        from .shell_tool import _stop_timed_out_process
                        _stop_timed_out_process(process)
                    else:
                        process.kill()
                    process.communicate()
                    raise
            if process.returncode not in accepted:
                # Do not echo credential-helper or remote URL errors into prompts.
                raise ValueError(f"Git {arguments[0]} failed with exit code {process.returncode}")
            return stdout.decode("utf-8", errors="replace").strip()
        if Path(remote).is_absolute() and git("-C", remote, "rev-parse", "--is-bare-repository") != "true":
            raise ValueError("Local Git target must be a dedicated bare test repository")
        if git("ls-remote", "--heads", remote, "refs/heads/" + branch):
            raise ValueError("Test branch already exists; choose a fresh branch")
        git("init", "--quiet", "--initial-branch=" + branch)
        # A new isolated branch is a complete submitted snapshot, not a merge
        # or modification of the designated repository's main branch.
        for name, data in snapshots.items():
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        git("add", "--", *paths)
        git("-c", "user.name=MiniAgent Testpack", "-c", "user.email=testpack@example.invalid",
            "commit", "--quiet", "-m", "Submit reviewed testpack snapshot")
        commit = git("rev-parse", "HEAD")
        action = {"operation": "create_test_branch", "remote": remote, "branch": branch,
                  "commit": commit, "files": hashes}
        digest = hashlib.sha256(json.dumps(action, sort_keys=True).encode()).hexdigest()
        prompt = "批准把以下已准备的快照发布到独立 Git 测试仓库？\n" + json.dumps(action, ensure_ascii=False, indent=2)
        if ctx.approve is None or not ctx.approve(prompt):
            return ToolResult.failure("Git publication denied; no remote write was performed.",
                                      decision="deny", action_digest=digest, commit=commit, files=hashes)
        # A concurrently created branch is not part of this approval. The
        # empty expected ref makes this create-only even across that race.
        git("push", "--porcelain", "--force-with-lease=refs/heads/" + branch + ":", remote,
            commit + ":refs/heads/" + branch)
        observed = git("ls-remote", "--heads", remote, "refs/heads/" + branch)
        if not observed.startswith(commit + "\t"):
            return ToolResult.failure("Push returned, but remote commit verification failed.", commit=commit)
        return ToolResult.success("Published and verified test snapshot " + commit, decision="allow",
                                  action_digest=digest, commit=commit, files=hashes, branch=branch, exit_code=0)
