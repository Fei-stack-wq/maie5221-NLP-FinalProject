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
        raise ValueError("请选择项目内可见的文件或目录")
    if not (scope.is_file() or scope.is_dir()):
        raise ValueError("请选择普通文件或目录")
    return scope


def _page(ctx, args, *, filenames):
    requested = args.get("path", ".")
    scope = _scope(ctx, requested)
    if filenames and not scope.is_dir():
        return ToolResult.failure("find_files 的 path 必须是目录")
    limit, offset = args.get("limit", 40), args.get("offset", 0)
    if not 1 <= limit <= 150 or not 0 <= offset <= 1_000_000:
        return ToolResult.failure("limit 范围为 1..150，offset 为 0..1000000")
    binary = _rg_path()
    if not binary:
        return ToolResult.failure("缺少 rg。请安装项目的 requirements.txt 或系统 ripgrep。")
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
            return ToolResult.failure("query 须为 1..4096 个字符，context 范围为 0..8")
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
            return ToolResult.failure("搜索超时，请缩小 path 或 glob", timed_out=True)
        if not more and proc.returncode not in (0, 1):
            diagnostic.seek(0)
            error = diagnostic.read(1500).decode("utf-8", "replace").strip()
            return ToolResult.failure(f"ripgrep 搜索失败：{error}", exit_code=proc.returncode)
    next_offset = offset + len(rows) if more else None
    output = "\n".join(rows) or "（没有结果）"
    if more:
        output += f"\n[下一页 offset={next_offset}]"
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
        return ToolResult.failure("start_line 从 1 开始，limit 为 1..400 行")
    data, _ = _load_for_edit(path)
    try:
        source = data.decode("utf-8")
        lines = source.splitlines(keepends=True)
    except UnicodeDecodeError:
        return ToolResult.failure("请选择 UTF-8 文本文件")
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
            rows.append(prefix + f" … [长行；完整内容可用 read_file 从 offset={char_end} 分页读取]")
            char_end += len(prefix) - len(label)
            break
        rows.append(row)
        char_end += len(line)
        used += len(row) + 1
    next_line = start + len(rows) if start + len(rows) <= len(lines) else None
    digest = hashlib.sha256(data).hexdigest()
    output = "\n".join(rows) or "（已到文件末尾）"
    footer = f"\n[总行数 {len(lines)}，sha256={digest}"
    if next_line is not None:
        footer += f"，下一页 start_line={next_line}"
    return ToolResult.success(output + footer + "]", path=args["path"], start_line=start,
                              next_line=next_line, total_lines=len(lines), sha256=digest,
                              chars=len(source), char_start=char_start, char_end=char_end,
                              returned_lines=len(rows) - int(partial_line is not None), partial_line=partial_line)
