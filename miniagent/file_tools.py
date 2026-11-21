"""Bounded text reads and optimistic, atomic workspace file mutations.

These are filesystem helpers, not an execution sandbox. Paths still use the
existing Sandbox boundary. Atomic replacement avoids partial writes; a version
check and in-process lock detect ordinary concurrent edits, not a cross-process
compare-and-swap transaction.
"""
from __future__ import annotations

import codecs
import hashlib
import os
import re
import stat
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from .schemas import ToolResult

if TYPE_CHECKING:
    from .tools import ToolContext

DEFAULT_READ_CHARS = 32_768
MAX_READ_CHARS = 65_536
MAX_WRITE_CHARS = 1_000_000
MAX_EDIT_BYTES = 8 * 1024 * 1024
_CHUNK_BYTES = 65_536
_LOCKS = tuple(threading.RLock() for _ in range(64))


class FileChangedError(ValueError):
    pass


def _lock(path: Path):
    return _LOCKS[hash(str(path)) % len(_LOCKS)]


def _version(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _path_version(path: Path):
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("目标必须是普通文件")
    return _version(info)


def _expected(args: dict, actual: str | None) -> ToolResult | None:
    expected = args.get("expected_sha256")
    if expected is None:
        return None
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        return ToolResult.failure("参数校验失败：expected_sha256 必须是 64 位十六进制摘要。")
    if expected.lower() != actual:
        return ToolResult.failure("文件版本冲突：内容已变化，请重新 read_file 后再编辑。",
                                  code="file_conflict", sha256=actual)
    return None


def _no_change(path: str, sha256: str) -> ToolResult:
    return ToolResult.failure(f"[no_change] {path} 内容未变化，已跳过写入（sha256={sha256}）。",
                              code="no_change", changed=False, path=path, sha256=sha256)


def _atomic_replace(path: Path, data: bytes, version) -> None:
    # Temporary files are colocated so replacement is atomic on the same volume.
    descriptor, temporary = tempfile.mkstemp(prefix=".miniagent-write-", dir=path.parent)
    try:
        if version is not None:
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if _path_version(path) != version:
            raise FileChangedError("文件在写入前发生变化，请重新读取后重试。")
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def _load_for_edit(path: Path) -> tuple[bytes, tuple]:
    # The size guard precedes allocation; read(limit+1) handles concurrent growth.
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("目标必须是普通文件")
    if info.st_size > MAX_EDIT_BYTES:
        raise ValueError(f"文件超过编辑上限 {MAX_EDIT_BYTES} 字节，请拆分文件。")
    with path.open("rb") as stream:
        before = _version(os.fstat(stream.fileno()))
        data = stream.read(MAX_EDIT_BYTES + 1)
        after = _version(os.fstat(stream.fileno()))
    if len(data) > MAX_EDIT_BYTES:
        raise ValueError(f"文件超过编辑上限 {MAX_EDIT_BYTES} 字节，请拆分文件。")
    if before != after or _path_version(path) != before:
        raise FileChangedError("读取过程中文件发生变化，请重新读取后重试。")
    return data, before


def read_file(ctx: ToolContext, args: dict) -> ToolResult:
    """Read a Unicode-character page; hash the stream without buffering the file."""
    offset, limit = args.get("offset", 0), args.get("limit", DEFAULT_READ_CHARS)
    if type(offset) is not int or not 0 <= offset <= 2**63 - 1:
        return ToolResult.failure("参数校验失败：offset 必须是从 0 开始的非负字符位置。")
    if type(limit) is not int or not 1 <= limit <= MAX_READ_CHARS:
        return ToolResult.failure(f"参数校验失败：limit 必须是 1..{MAX_READ_CHARS} 的整数。")
    path = ctx.sandbox.resolve(args["path"], must_exist=True)
    if not path.is_file():
        return ToolResult.failure(f"不是文件：{args['path']}")
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    digest, pieces, total_chars = hashlib.sha256(), [], 0
    with _lock(path), path.open("rb") as stream:
        before = _version(os.fstat(stream.fileno()))
        try:
            while True:
                block = stream.read(_CHUNK_BYTES)
                digest.update(block)
                text = decoder.decode(block, final=not block)
                start, stop = max(0, offset - total_chars), min(len(text), offset + limit - total_chars)
                if stop > start:
                    pieces.append(text[start:stop])
                total_chars += len(text)
                if not block:
                    break
        except UnicodeDecodeError:
            return ToolResult.failure("该文件不是 UTF-8 文本，无法读取。")
        if before != _version(os.fstat(stream.fileno())) or _path_version(path) != before:
            return ToolResult.failure("读取过程中文件发生变化，请重试。", code="file_conflict")
    text = "".join(pieces)
    has_more = offset + len(text) < total_chars
    next_offset = offset + len(text) if has_more else None
    sha256 = digest.hexdigest()
    footer = f"\n[文件版本 sha256={sha256}；字符范围 {offset}..{offset + len(text)}；总字符 {total_chars}。"
    if has_more:
        footer += f"已截断；继续读取：read_file(path=原路径, offset={next_offset}, limit={limit})]"
    else:
        footer += "已到文件末尾。]"
    return ToolResult.success(text + footer, path=args["path"], chars=total_chars,
                              returned_chars=len(text), bytes=before[2], sha256=sha256,
                              offset=offset, next_offset=next_offset, eof=not has_more,
                              truncated=has_more)


def write_file(ctx: ToolContext, args: dict) -> ToolResult:
    content = args["content"]
    if len(content) > MAX_WRITE_CHARS:
        return ToolResult.failure(f"内容过长（上限 {MAX_WRITE_CHARS} 字符）；创建首段后使用 append_file，或拆分文件。")
    path = ctx.sandbox.resolve(args["path"])
    data = content.encode("utf-8")
    sha256 = hashlib.sha256(data).hexdigest()
    with _lock(path):
        version = _path_version(path)
        actual = None
        if version is not None and ("expected_sha256" in args or version[2] == len(data)):
            current, version = _load_for_edit(path)
            actual = hashlib.sha256(current).hexdigest()
        conflict = _expected(args, actual)
        if conflict:
            return conflict
        if actual == sha256:
            return _no_change(args["path"], sha256)
        ctx.sandbox.ensure_parent(path)
        _atomic_replace(path, data, version)
    return ToolResult.success(f"已写入 {args['path']}（{len(content)} 字符，sha256={sha256}）",
                              changed=True, bytes=len(data), path=args["path"], sha256=sha256, artifacts=[args["path"]])


def edit_file(ctx: ToolContext, args: dict) -> ToolResult:
    old, new = args["old_text"], args["new_text"]
    if not old or max(len(old), len(new)) > MAX_WRITE_CHARS:
        return ToolResult.failure(f"参数校验失败：old_text 不能为空；替换文本最多 {MAX_WRITE_CHARS} 字符。")
    path = ctx.sandbox.resolve(args["path"], must_exist=True)
    with _lock(path):
        data, version = _load_for_edit(path)
        original_hash = hashlib.sha256(data).hexdigest()
        conflict = _expected(args, original_hash)
        if conflict:
            return conflict
        text = data.decode("utf-8")
        occurrences = text.count(old)
        # count() skips overlaps; an overlapping second match is also ambiguous.
        first = text.find(old)
        ambiguous = first >= 0 and text.find(old, first + 1) >= 0
        if occurrences != 1 or ambiguous:
            return ToolResult.failure("替换未执行：old_text 必须在原文件中恰好匹配一次；请读取并增加匹配上下文。",
                                      code="ambiguous_edit" if occurrences else "text_not_found",
                                      matches=max(occurrences, 2 if ambiguous else 0), sha256=original_hash)
        if old == new:
            return _no_change(args["path"], original_hash)
        updated = (text[:first] + new + text[first + len(old):]).encode("utf-8")
        if len(updated) > MAX_EDIT_BYTES:
            return ToolResult.failure(f"替换后文件超过 {MAX_EDIT_BYTES} 字节，未修改原文件。")
        _atomic_replace(path, updated, version)
    sha256 = hashlib.sha256(updated).hexdigest()
    return ToolResult.success(f"已精确替换 {args['path']} 的 1 处内容（sha256={sha256}）",
                              changed=True, path=args["path"], sha256=sha256, previous_sha256=original_hash,
                              bytes=len(updated), artifacts=[args["path"]])


def append_file(ctx: ToolContext, args: dict) -> ToolResult:
    content = args["content"]
    if not content or len(content) > MAX_WRITE_CHARS:
        return ToolResult.failure(f"参数校验失败：追加内容必须非空且不超过 {MAX_WRITE_CHARS} 字符。")
    path = ctx.sandbox.resolve(args["path"], must_exist=True)
    with _lock(path):
        data, version = _load_for_edit(path)
        data.decode("utf-8")  # Do not silently append text to a non-text file.
        original_hash = hashlib.sha256(data).hexdigest()
        conflict = _expected(args, original_hash)
        if conflict:
            return conflict
        updated = data + content.encode("utf-8")
        if len(updated) > MAX_EDIT_BYTES:
            return ToolResult.failure(f"追加后文件超过 {MAX_EDIT_BYTES} 字节，未修改原文件。")
        _atomic_replace(path, updated, version)
    sha256 = hashlib.sha256(updated).hexdigest()
    return ToolResult.success(f"已追加到 {args['path']}（sha256={sha256}）",
                              changed=True, path=args["path"], sha256=sha256, previous_sha256=original_hash,
                              bytes=len(updated), artifacts=[args["path"]])
