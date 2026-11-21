"""Private, immutable originals for user-supplied Studio chat attachments."""
from __future__ import annotations

import errno
import json
import os
import re
import secrets
import stat
import unicodedata
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO


_STORE = ".studio-uploads"
_ID = re.compile(r"[a-f0-9]{32}")
_DIRECTORY = os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW
_CHUNK_SIZE = 256 * 1024
MAX_ATTACHMENTS = 100  # Message metadata must fit the Studio run's JSON record.


class UploadError(ValueError):
    def __init__(self, message: str, code: str = "invalid_attachment", status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def _name(value: str) -> str:
    if not isinstance(value, str):
        raise UploadError("附件文件名无效。")
    name = unicodedata.normalize("NFC", value)
    if (not name.strip() or name in (".", "..") or "/" in name or "\\" in name
            or any(unicodedata.category(ch) in {"Cc", "Cs"} for ch in name)
            or len(name.encode("utf-8")) > 255):
        raise UploadError("请使用有效的文件名，不能包含路径或控制字符。")
    return name


def validate_upload_ids(ids: list[str]) -> list[str]:
    if not isinstance(ids, list) or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in ids):
        raise UploadError("attachments 必须是上传成功后返回的附件 ID 列表。")
    if len(ids) > MAX_ATTACHMENTS:
        raise UploadError(f"每条消息最多发送 {MAX_ATTACHMENTS} 个附件。")
    if len(set(ids)) != len(ids):
        raise UploadError("一条消息中不能重复引用同一附件。")
    return ids


@contextmanager
def _directory(name, parent=None):
    fd = os.open(name, _DIRECTORY, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _store(runs_dir: Path, *, create=False):
    with _directory(runs_dir) as root:
        if create:
            with suppress(FileExistsError):
                os.mkdir(_STORE, mode=0o700, dir_fd=root)
        with _directory(_STORE, root) as directory:
            yield directory


def _create(directory: int, name: str):
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o400, dir_fd=directory)
    return os.fdopen(fd, "wb")


@contextmanager
def _pending(store: int, upload_id: str):
    name = "." + upload_id + ".pending"
    os.mkdir(name, mode=0o700, dir_fd=store)
    try:
        with _directory(name, store) as directory:
            try:
                yield directory
                os.rename(name, upload_id, src_dir_fd=store, dst_dir_fd=store)
            except BaseException:
                for part in ("data", "metadata.json"):
                    with suppress(FileNotFoundError):
                        os.unlink(part, dir_fd=directory)
                raise
    finally:
        with suppress(FileNotFoundError):
            os.rmdir(name, dir_fd=store)


def save_upload(runs_dir: Path, name: str, stream: BinaryIO, size: int) -> dict:
    name = _name(name)
    if type(size) is not int or size < 0:
        raise UploadError("附件大小无效。")
    upload_id = secrets.token_hex(16)
    ref = {"id": upload_id, "name": name, "size": size}
    try:
        with _store(runs_dir, create=True) as store, _pending(store, upload_id) as directory:
            with _create(directory, "data") as data:
                remaining = size
                while remaining:
                    chunk = stream.read(min(_CHUNK_SIZE, remaining))
                    if not chunk or len(chunk) > remaining:
                        raise UploadError("附件传输不完整，请重新上传。", "incomplete_upload")
                    data.write(chunk)
                    remaining -= len(chunk)
                data.flush()
                os.fsync(data.fileno())
            with _create(directory, "metadata.json") as metadata:
                metadata.write(json.dumps(ref, ensure_ascii=False).encode("utf-8"))
                metadata.flush()
                os.fsync(metadata.fileno())
    except (ConnectionError, TimeoutError):
        raise UploadError("附件传输已中断，请重新上传。", "incomplete_upload") from None
    except OSError as exc:
        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise UploadError("本地存储空间不足，附件未保存。", "insufficient_storage", 507) from None
        raise UploadError("本地附件目录不可写。", "attachment_storage", 409) from exc
    return ref


def _read(directory: int, name: str):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UploadError("附件存储格式无效。", "attachment_storage", 409)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def open_upload(runs_dir: Path, upload_id: str) -> tuple[dict, BinaryIO]:
    """Return verified server metadata and an open original. Caller closes the stream."""
    validate_upload_ids([upload_id])
    try:
        with _store(runs_dir) as store, _directory(upload_id, store) as entry:
            with _read(entry, "metadata.json") as metadata:
                raw = metadata.read(4097)
            if len(raw) > 4096:
                raise ValueError()
            ref = json.loads(raw)
            if (not isinstance(ref, dict) or set(ref) != {"id", "name", "size"} or ref["id"] != upload_id
                    or _name(ref["name"]) != ref["name"] or type(ref["size"]) is not int or ref["size"] < 0):
                raise ValueError()
            file = _read(entry, "data")
            try:
                if os.fstat(file.fileno()).st_size != ref["size"]:
                    raise ValueError()
                return ref, file
            except BaseException:
                file.close()
                raise
    except FileNotFoundError:
        raise UploadError("找不到该附件，请重新选择文件。", "attachment_not_found", 404) from None
    except (OSError, ValueError) as exc:
        raise UploadError("附件内容或元数据损坏，无法读取。", "attachment_storage", 409) from exc


def resolve_uploads(runs_dir: Path, ids: list[str]) -> list[dict]:
    refs = []
    for upload_id in validate_upload_ids(ids):
        ref, stream = open_upload(runs_dir, upload_id)
        stream.close()
        refs.append(ref)
    return refs
