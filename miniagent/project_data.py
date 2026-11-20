"""Supported project data for immutable workspace snapshots and ZIP downloads."""
from __future__ import annotations

import hashlib
import io
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

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
# On its official English newsroom the publisher uses its own name as the
# first two words of press-release slugs. Recognize just that leading brand
# marker, only when the rest has a bounded, ordinary headline structure.
_NEWSROOM_PREFIX = b"https://news.skhynix.com/en/"
_HEADLINE_WORD = re.compile(rb"(?:[a-z]{1,18}|[a-z]{1,6}[0-9]{1,4}[a-z]{0,2}|[1-4]q[0-9]{2,4}|[0-9]{1,6}(?:st|nd|rd|th)?)")
_HEADLINE_CONNECTORS = frozenset(b"a an and as at by for from in into of on the to with".split())
_URL_FIELD = re.compile(rb'(?:\\?"(?:url|html_url)\\?"\s*:\s*\\?")$')
_TASKSTORE_SCHEMA = (b"CREATE TABLE creation_keys", b"CREATE INDEX records_task", b"CREATE INDEX outbox_pending")


def _is_task_section_word(data: bytes, start: int) -> bool:
    """Only the 'sk-' starting inside a corroborated Markdown section word."""
    if start < 2 or data[start - 2:start + 3] != b"task-":
        return False
    # The bounded link grammar above fits inside 1 KiB.
    opening = data.rfind(b"[", max(0, start - 1024), start - 2)
    if opening < 0:
        return False
    link = _SECTION_LINK.match(data, opening)
    if not link or not link.start(2) <= start - 2 < link.end(2):
        return False
    words = link[1].lower().replace(b" ", b"-")
    if words != link[2]:
        return False
    offset = start - 2 - link.start(2)
    return ((not offset or words[offset - 1:offset] == b"-") and
            words[offset:].startswith(b"task-") and words[offset:].count(b"-") >= 2)


def _is_url_boundary(data: bytes, start: int, end: int) -> bool:
    before = data[max(0, start - 1):start]
    after = data[end:end + 1]
    if before and before not in b""" \t\r\n<[(`'"|""":
        return False
    if after and after not in b""" \t\r\n>])`'"|?#""" and not data.startswith(b'\\"', end):
        return False
    # Parentheses and apostrophes are legal characters in an enclosing URL.
    # Never recognize an inner URL as a separate citation in its query.
    if before and before in b"(['":
        left = start - 1
        while left > 0 and data[left - 1] not in b' \t\r\n<>"`|':
            left -= 1
            if start - left > 512:
                return False
        if b"://" in data[left:start]:
            return False
    return True


def _is_public_newsroom_title(data: bytes, match: re.Match[bytes]) -> bool:
    start, end = match.span()
    brand = b"sk-hynix-"
    if end - start > 200 or not data.startswith(brand, start) or data[end:end + 1] != b"/":
        return False
    url_start = start - len(_NEWSROOM_PREFIX)
    if url_start < 0 or data[url_start:start] != _NEWSROOM_PREFIX:
        return False
    if not _is_url_boundary(data, url_start, end + 1):
        return False
    words = data[start + len(brand):end].split(b"-")
    return (4 <= len(words) <= 32 and all(_HEADLINE_WORD.fullmatch(word) for word in words)
            and sum(word.isalpha() and len(word) > 1 for word in words) >= 3
            and (not _HEADLINE_CONNECTORS.isdisjoint(words)
                 or any(re.fullmatch(rb"(?:[1-4]q|fy)[0-9]{2,4}", word) for word in words)))


def _title_marker(title: str, url: str) -> int | None:
    """Corroborate one 'sk-' inside a publisher name or a longer title word."""
    # No authority credentials, escapes, ports, query data or nested URLs in
    # this evidence. Only a complete HTTPS permalink can identify a title.
    if not re.fullmatch(r"https://(?:[a-z0-9-]+\.)+[a-z]{2,24}/(?:[a-zA-Z0-9._~-]+/)*"
                        r"[a-z0-9-]{1,250}(?:\.html?)?/?", url):
        return None
    path = urlsplit(url).path.rstrip("/")
    if set(path.split("/")) & {".", ".."}:
        return None
    slug = path.rsplit("/", 1)[-1].removesuffix(".html").removesuffix(".htm")
    token = re.search(r"(?:^|-)([a-z]{2,16}sk|sk)-", slug)
    if not token:
        return None
    words = slug[token.start(1):].split("-")
    if not (4 <= len(words) <= 32 and all(_HEADLINE_WORD.fullmatch(w.encode()) for w in words)):
        return None
    if words[0] == "sk" and words[1] not in {"hynix", "hynixs"}:
        return None
    if sum(w.isalpha() and len(w) >= 3 for w in words) < 3:
        return None
    # Headlines and permalinks may omit punctuation, articles and a possessive
    # apostrophe. Require every remaining slug word in the original title, in order.
    normalized = re.sub(r"(?<=\w)\s*['’]\s*s\b", "s", title.lower())
    if words[:2] == ["sk", "hynixs"]:
        words[1] = "hynix"
        normalized = re.sub(r"\bhynixs\b", "hynix", normalized)
    title_words = re.findall("[a-z0-9]+", normalized)
    if words[0] == "sk" and tuple(words[:2]) not in zip(title_words, title_words[1:]):
        return None
    visible = iter(title_words)
    if not all(w in visible for w in words):
        return None
    return url.rindex("/" + slug) + 1 + token.end(1) - 2


def _archived_title_offsets(name: str, data: bytes) -> set[int]:
    # A digest proves byte identity, not provenance. Independently constrain
    # the record shape, title, URL component and exact location of the marker.
    if not re.fullmatch(r"sources/[a-f0-9]{64}\.json", name) or Path(name).stem != hashlib.sha256(data).hexdigest():
        return set()
    try:
        record = json.loads(data)
        # _save writes this exact JSON encoding. With ASCII-only candidate
        # permalinks, each accepted field must therefore occur literally once.
        if not isinstance(record, dict) or json.dumps(record, ensure_ascii=False, indent=2).encode() != data:
            return set()
        if record.get("kind") == "source_discovery":
            rows = record.get("results")
        elif record.get("kind") == "fetched_source" and "json" in str(record.get("content_type", "")):
            body = record.get("text")
            if not isinstance(body, str) or re.search(r"\\(?:/|u00[0-7][0-9a-fA-F])", body):
                return set()
            response = json.loads(body)
            rows = response.get("results") if isinstance(response, dict) else None
        else:
            return set()
    except (ValueError, TypeError, RecursionError):
        return set()
    offsets = set()
    if not isinstance(rows, list):
        return offsets
    counts = Counter()
    for row in rows[:200]:
        if not isinstance(row, dict):
            continue
        url, title = row.get("html_url") or row.get("url"), row.get("title")
        if not isinstance(url, str) or not isinstance(title, str) or len(url) > 2048 or len(title) > 1024:
            continue
        marker = _title_marker(title, url)
        if marker is None:
            continue
        counts[(url.encode("ascii"), marker)] += 1
    for (literal, marker), count in counts.items():
        # A copy under another field or at a different position cannot borrow
        # the genuine title/URL pair. Keep all such occurrences blocked.
        if data.count(literal) != count:
            continue
        offset = 0
        while (start := data.find(literal, offset)) >= 0:
            end = start + len(literal)
            if _URL_FIELD.search(data, max(0, start - 48), start) and _is_url_boundary(data, start, end):
                offsets.add(start + marker)
            offset = end
    return offsets


def _text_credential_offset(name: str, data: bytes) -> int | None:
    offset = 0
    archived = None
    while match := _KEY_PATTERN.search(data, offset):
        start = match.start()
        canonical_task = start >= 2 and _STUDIO_TASK_ID.match(data, start - 2)
        if not (canonical_task or _is_task_section_word(data, start) or _is_public_newsroom_title(data, match)):
            if archived is None:
                archived = _archived_title_offsets(name, data)
            if start not in archived:
                return start
        # The generic pattern is greedy. A separate token may begin inside
        # its match, even elsewhere in the same anchor. Rescan that suffix.
        offset = match.start() + 3
    return None


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
        configured = data.find(secret.encode()) if secret else -1
        offset = configured if configured >= 0 else _text_credential_offset(name, data)
        if offset is not None:
            line = data.count(b"\n", 0, offset) + 1
            raise ValueError(f"项目文件含可识别的服务凭据（第 {line} 行）。")
    if b"\x00" in data:
        raise ValueError("项目文本包含二进制数据。")
    try:
        data.decode("utf-8")
    except UnicodeError:
        raise ValueError("项目文本必须是 UTF-8。") from None
