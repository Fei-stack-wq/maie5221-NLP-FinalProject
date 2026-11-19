"""Supported project data for immutable workspace snapshots and ZIP downloads."""
from __future__ import annotations

import io
import re
from pathlib import Path

# Ignored only as directories. A link masquerading as a cache in a
# publishable project must still pass through the normal link rejection.
PROJECT_CACHE_DIRS = frozenset({"__pycache__", "node_modules", "venv"})


def supported_project_file(name, formats):
    path = Path(name)
    return path.suffix.lower() in formats or path.name in formats


SQLITE_EXTENSIONS = {".db", ".sqlite", ".sqlite3"}
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
# SQLite pages can retain deleted values. Inspect the full byte image; SQL
# queries on live rows alone could miss previously stored provider secrets.
_KEY_PATTERN = re.compile(rb"(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}|github_pat_[A-Za-z0-9_]{60,}|"
                          rb"gh[pousr]_[A-Za-z0-9]{36}|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----)")
# Studio writes task-<UUID hex> into citations and handoff reports. Only
# exclude a complete canonical identifier from generic text pattern matching.
_STUDIO_TASK_ID = re.compile(rb"(?<![A-Za-z0-9_])task-[0-9a-f]{32}(?![A-Za-z0-9_-])")
# A Markdown section link can reproduce the label's words after "#", with
# an optional section number. Keep this grammar narrower than URL syntax.
_SECTION_LINK = re.compile(rb"\[([A-Za-z]{1,24}(?:[ -][A-Za-z]{1,24}){2,15})\]\("
                           rb"#(?:[0-9]{1,8}-)?([a-z]{1,24}(?:-[a-z]{1,24}){2,15})\)")
_TASKSTORE_SCHEMA = (b"CREATE TABLE creation_keys", b"CREATE INDEX records_task", b"CREATE INDEX outbox_pending")


def _is_task_section_word(data: bytes, start: int) -> bool:
    """Only the 'sk-' starting inside a corroborated Markdown section word."""
    if start < 2 or data[start-2:start+1] != b"tas":
        return False
    opening = data.rfind(b"[", max(0, start-820), start-2)
    if opening < 0:
        return False
    link = _SECTION_LINK.match(data, opening)
    if not link or not link.start(2) <= start-2 < link.end(2):
        return False
    words = link[1].lower().replace(b" ", b"-")
    if words != link[2]:
        return False
    offset = start - 2 - link.start(2)
    return ((not offset or words[offset-1:offset] == b"-") and
            words[offset:].startswith(b"task-") and words[offset:].count(b"-") >= 2)


def _has_text_credential(data: bytes) -> bool:
    if not _KEY_PATTERN.search(data):
        return False
    text = _STUDIO_TASK_ID.sub(b"[task]", data)
    offset = 0
    while match := _KEY_PATTERN.search(text, offset):
        if not _is_task_section_word(text, match.start()):
            return True
        # The generic pattern is greedy. A separate token may begin inside
        # its match, even elsewhere in the same anchor. Rescan that suffix.
        offset = match.start() + 3
    return False


def is_database(name: str) -> bool:
    return Path(name).suffix.lower() in SQLITE_EXTENSIONS


def is_png(name: str) -> bool:
    return Path(name).suffix.lower() == ".png"


def _validate_png(data: bytes, secret: str) -> None:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data), formats=["PNG"]) as img:
            if img.width * img.height > 16_000_000 or getattr(img, "is_animated", False):
                raise ValueError("截图须为最多 1600 万像素的静态 PNG")
            img.verify()
        # Keep the canonical file bytes, including checked chunks. Appended
        # unrelated data must not hitch a ride in a binary download.
        cursor = 8
        while cursor + 12 <= len(data):
            length = int.from_bytes(data[cursor:cursor+4], "big")
            kind = data[cursor+4:cursor+8]
            cursor += length + 12
            if kind == b"IEND":
                break
        if cursor != len(data) or not data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82"):
            raise ValueError("PNG 文件结束位置无效")
        with Image.open(io.BytesIO(data), formats=["PNG"]) as img:
            img.load()
            metadata = repr(img.info).encode("utf-8")
        if _KEY_PATTERN.search(data) or _KEY_PATTERN.search(metadata) or (
                secret and any(secret.encode() in value for value in (data, metadata))):
            raise ValueError("PNG 文件包含服务凭据")
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("截图不是有效的 PNG 文件") from exc


def validate_project_data(name: str, data: bytes, *, secret: str = "", reject_credentials: bool = False) -> None:
    """Recognize a standalone SQLite file without executing database content."""
    if is_png(name):
        _validate_png(data, secret)
        return
    if is_database(name):
        if len(data) < 100 or not data.startswith(b"SQLite format 3\x00"):
            raise ValueError("项目数据库不是有效的 SQLite 3 文件。")
        page_size = int.from_bytes(data[16:18], "big")
        if page_size == 1:
            page_size = 65_536
        if (not 512 <= page_size <= 65_536 or page_size & (page_size - 1)
                or len(data) % page_size or data[18] not in (1, 2) or data[19] not in (1, 2)):
            raise ValueError("SQLite 文件头或页长度无效，无法完整交接。")
        if all(signature in data for signature in _TASKSTORE_SCHEMA):
            raise ValueError("检测到 Studio 任务控制数据库，已拒绝交接。")
        # Scan both ordinary bytes and zero-padded ASCII in UTF-16 fields;
        # SQLite cell payloads can start at an odd offset within their page.
        if _KEY_PATTERN.search(data.replace(b"\0", b"")):
            raise ValueError("数据库含可识别的服务凭据，已拒绝交接。")
        if secret and any(secret.encode(encoding) in data for encoding in ("utf-8", "utf-16le", "utf-16be")):
            raise ValueError("数据库包含当前服务凭据，已拒绝交接。")
        return
    if reject_credentials:
        configured_key = secret and secret.encode() in data
        if configured_key or _has_text_credential(data):
            raise ValueError("项目文件含可识别的服务凭据。")
    if b"\x00" in data:
        raise ValueError("项目文本包含二进制数据。")
    try:
        data.decode("utf-8")
    except UnicodeError:
        raise ValueError("项目文本必须是 UTF-8。") from None
