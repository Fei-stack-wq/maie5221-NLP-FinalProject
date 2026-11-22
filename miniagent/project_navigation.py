"""Read-only project navigation, using the same ripgrep executable as the terminal."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .file_tools import _load_for_edit
from .project_data import PROJECT_CACHE_DIRS
from .schemas import ToolResult


def _rg_path():
    name = "rg.exe" if os.name == "nt" else "rg"
    bundled = Path(sys.executable).parent / name
    return str(bundled) if bundled.is_file() else shutil.which(name)


def _scope(ctx, requested):
    scope = ctx.sandbox.resolve(requested, must_exist=True)
    if any(part.startswith(".") for part in scope.relative_to(ctx.sandbox.root).parts):
        raise ValueError("Choose a visible workspace file or directory")
    if not (scope.is_file() or scope.is_dir()):
        raise ValueError("Choose a regular file or directory")
    return scope


def _page(ctx, args, *, filenames):
    requested = args.get("path", ".")
    scope = _scope(ctx, requested)
    if filenames and not scope.is_dir():
        return ToolResult.failure("find_files requires a directory as its path")
    limit, offset = args.get("limit", 40), args.get("offset", 0)
    if not 1 <= limit <= 150 or not 0 <= offset <= 1_000_000:
        return ToolResult.failure("limit must be 1..150 and offset 0..1000000")
    binary = _rg_path()
    if not binary:
        return ToolResult.failure("ripgrep is missing. Install the repository requirements or a system copy of rg.")
    cmd = [binary, "--no-config", "--no-follow", "--no-require-git", "--no-ignore-parent",
           "--no-ignore-global", "--sort", "path", "--color", "never"]
    pattern = args.get("pattern" if filenames else "glob")
    if pattern:
        cmd.extend(["--glob", pattern])
    for exclude in (".*", "**/.*/**", *(f"**/{name}/**" for name in sorted(PROJECT_CACHE_DIRS))):
        cmd.extend(["--glob", "!" + exclude])
    if filenames:
        cmd.append("--files")
    else:
        query, context = args["query"], args.get("context", 0)
        if not query or len(query) > 4096 or not 0 <= context <= 8:
            return ToolResult.failure("query must contain 1..4096 characters and context must be 0..8")
        cmd.extend(["--with-filename", "--line-number", "--no-heading", "--no-context-separator",
                    "--max-columns", "320", "--max-columns-preview", "--context", str(context)])
        if args.get("literal"):
            cmd.append("--fixed-strings")
        if args.get("ignore_case"):
            cmd.append("--ignore-case")
        cmd.extend(["-e", query])
    cmd.extend(["--", ctx.sandbox.relative(scope)])

    rows = []
    size = 0
    more = False
    expired = threading.Event()
    with tempfile.TemporaryFile() as diagnostic, subprocess.Popen(
        cmd, cwd=ctx.sandbox.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=diagnostic, text=True, encoding="utf-8", errors="replace",
    ) as proc:
        def expire():
            expired.set()
            proc.kill()
        timer = threading.Timer(max(0.1, min(20, ctx.limits.tool_timeout_seconds)), expire)
        timer.daemon = True
        timer.start()
        try:
            for index, line in enumerate(proc.stdout):
                if index < offset:
                    continue
                row = line.removeprefix("./").rstrip("\r\n")
                if len(rows) >= limit or (rows and size + len(row) > 12_000):
                    more = True
                    proc.kill()
                    break
                rows.append(row)
                size += len(row) + 1
            proc.wait()
        finally:
            timer.cancel()
            proc.stdout.close()
        if expired.is_set():
            return ToolResult.failure("Search timed out. Narrow the path or glob.", timed_out=True)
        if not more and proc.returncode not in (0, 1):
            diagnostic.seek(0)
            error = diagnostic.read(1500).decode("utf-8", "replace").strip()
            return ToolResult.failure(f"ripgrep failed: {error}", exit_code=proc.returncode)
    next_offset = offset + len(rows) if more else None
    output = "\n".join(rows) or "(no results)"
    if more:
        output += f"\n[next offset={next_offset}]"
    return ToolResult.success(output, path=requested, count=len(rows), offset=offset,
                              next_offset=next_offset, truncated=more)


def search_files(ctx, args):
    return _page(ctx, args, filenames=False)


def find_files(ctx, args):
    return _page(ctx, args, filenames=True)


def read_lines(ctx, args):
    path = _scope(ctx, args["path"])
    start, count = args.get("start_line", 1), args.get("limit", 120)
    if start < 1 or not 1 <= count <= 400:
        return ToolResult.failure("start_line starts at 1 and limit must be 1..400 lines")
    data, _ = _load_for_edit(path)
    try:
        source = data.decode("utf-8")
        lines = source.splitlines(keepends=True)
    except UnicodeDecodeError:
        return ToolResult.failure("Choose a UTF-8 text file")
    rows = []
    used = 0
    char_start = sum(map(len, lines[:start - 1]))
    char_end = char_start
    partial_line = None
    for number, line in enumerate(lines[start-1:start-1+count], start):
        label = f"{number:>5}  "
        row = label + line.rstrip("\r\n")
        if rows and used + len(row) > 16_000:
            break
        if len(row) > 16_000:
            prefix = row[:15_800]
            partial_line = number
            rows.append(prefix + f" … [long line, use read_file from offset={char_end}]")
            char_end += len(prefix) - len(label)
            break
        rows.append(row)
        char_end += len(line)
        used += len(row) + 1
    next_line = start + len(rows) if start + len(rows) <= len(lines) else None
    digest = hashlib.sha256(data).hexdigest()
    output = "\n".join(rows) or "(end of file)"
    footer = f"\n[total lines {len(lines)}, sha256={digest}"
    if next_line is not None:
        footer += f", next start_line={next_line}"
    return ToolResult.success(output + footer + "]", path=args["path"], start_line=start,
                              next_line=next_line, total_lines=len(lines), sha256=digest,
                              chars=len(source), char_start=char_start, char_end=char_end,
                              returned_lines=len(rows) - int(partial_line is not None), partial_line=partial_line)
