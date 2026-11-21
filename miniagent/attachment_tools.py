"""Read a run's authorized chat inputs independently of its publishable project tree."""
from __future__ import annotations

import base64
import codecs
import io
import json
import mimetypes
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
import zipfile
from contextlib import ExitStack, closing
from pathlib import Path

from .file_tools import MAX_READ_CHARS
from .research import _pdf_text
from .schemas import ToolResult
from .studio_workspace import _directory
from .tools import Tool

# Inline document parsers may load full ZIP parts and XML trees. Upload and
# materialization stream independently of this in-process preview budget.
_PREVIEW_BYTES = 32 * 1024**2
_OFFICE_EXT = {".docx", ".xlsx", ".pptx"}
_INPUT_DIR = ".studio-inputs"


class _PreviewError(ValueError):
    """A diagnostic safe to present alongside untrusted file data."""


def _page(parts, offset, limit):
    """Read at most one page plus lookahead, even for an open-ended iterator."""
    pieces = []
    pos = 0
    stop = offset + limit + 1
    for part in parts:
        if pos < stop and pos + len(part) > offset:
            pieces.append(part[max(0, offset - pos):stop - pos])
        pos += len(part)
        if pos >= stop:
            break
    if pos < offset:
        raise _PreviewError("offset 超出可读内容。")
    text = "".join(pieces)
    return text[:limit], offset + limit if len(text) > limit else None


def _text_blocks(stream):
    header = stream.read(512)
    stream.seek(0)
    if header.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif header.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        if any(c < 32 and c not in (9, 10, 12, 13) for c in header):
            raise _PreviewError("该附件是二进制文件。可使用 format=base64 读取字节范围，或在执行任务时调用 materialize_attachment。")
        encoding = "utf-8-sig"
    with io.TextIOWrapper(stream, encoding=encoding, newline="") as reader:
        while block := reader.read(16384):
            yield block


def _office_blocks(stream, suffix):
    with zipfile.ZipFile(stream) as package:
        if sum(entry.file_size for entry in package.infolist()) > _PREVIEW_BYTES * 2:
            raise _PreviewError("Office 包的解压体积超过内联预览容量，可以复制到成员工作区后处理。")
    stream.seek(0)
    if suffix == ".docx":
        from docx import Document
        from docx.table import Table
        for block in Document(stream).iter_inner_content():
            if isinstance(block, Table):
                for row in block.rows:
                    yield "\t".join(cell.text for cell in row.cells) + "\n"
            else:
                yield block.text + "\n"
    elif suffix == ".xlsx":
        from openpyxl import load_workbook
        with closing(load_workbook(stream, read_only=True, data_only=False, keep_links=False)) as book:
            for sheet in book.worksheets:
                yield f"[工作表 {sheet.title}]\n"
                sheet.reset_dimensions()
                for row in sheet.iter_rows(values_only=True):
                    yield "\t".join("" if value is None else str(value) for value in row) + "\n"
    else:
        from pptx import Presentation
        for number, slide in enumerate(Presentation(stream).slides, start=1):
            yield f"[幻灯片 {number}]\n"
            yield from _slide_text(slide.shapes)


def _slide_text(shapes):
    for shape in shapes:
        if shape.has_table:
            for row in shape.table.rows:
                yield "\t".join(cell.text for cell in row.cells) + "\n"
        elif shape.has_text_frame:
            yield shape.text + "\n"
        elif hasattr(shape, "shapes"):
            yield from _slide_text(shape.shapes)


def _image_text(stream, mode, timeout):
    from PIL import Image
    with Image.open(stream) as image:
        description = f"{image.format} 图像，{image.width} × {image.height} 像素，色彩模式 {image.mode}。"
        if mode != "ocr":
            return description + "可用 format=ocr 识别图中文字。"
        # Budget the decoder's full RGBA working raster before loading pixels,
        # independently of the size of the compressed file.
        if image.width * image.height * 4 > _PREVIEW_BYTES:
            raise _PreviewError("该图的像素数超过内联文字识别容量，可在成员工作区裁剪后处理。")
        # Re-encode only a recognized raster image for the OCR program.
        with tempfile.TemporaryFile() as raster:
            with image.convert("L") as grayscale:
                grayscale.save(raster, format="PNG")
            raster.seek(0)
            binary = shutil.which("tesseract")
            if not binary:
                raise _PreviewError("本机需安装 Tesseract 才能识别图中文字。")
            langs = subprocess.run([binary, "--list-langs"], capture_output=True, check=True,
                                   timeout=timeout).stdout.decode().splitlines()
            languages = [lang for lang in ("chi_sim", "chi_tra", "eng") if lang in langs]
            if not languages:
                raise _PreviewError("本机未安装中文或英文 Tesseract 语言包。")
            result = subprocess.run([binary, "stdin", "stdout", "-l", "+".join(languages)], stdin=raster,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, check=True)
            return description + "\nOCR（" + "+".join(languages) + "）\n" + (result.stdout.decode().strip() or "未识别到文字。")


def _preview(stream, name, size, args, ctx):
    offset, limit = args.get("offset", 0), args.get("limit", 4096)
    mode, page = args.get("format", "auto"), args.get("page")
    if offset < 0 or not 1 <= limit <= MAX_READ_CHARS:
        raise _PreviewError(f"offset 必须非负；limit 范围为 1..{MAX_READ_CHARS}。")
    if mode not in {"auto", "ocr", "base64"}:
        raise _PreviewError("format 可选 auto、ocr 或 base64。")
    if mode == "base64":
        if page is not None or offset > size:
            raise _PreviewError("base64 使用字节 offset，不支持页码；offset 不可超过文件大小。")
        stream.seek(offset)
        data = stream.read(limit)
        following = offset + len(data)
        return base64.b64encode(data).decode("ascii"), following if following < size else None, "字节（Base64）"
    header = stream.read(16)
    stream.seek(0)
    suffix = Path(name).suffix.lower()
    pdf = header.startswith(b"%PDF-")
    if page is not None and (not pdf or page < 1):
        raise _PreviewError("page 仅用于 PDF，从 1 开始。")
    image = (mimetypes.guess_type(name)[0] or "").startswith("image/") and suffix != ".svg"
    if mode == "ocr" and not image:
        raise _PreviewError("format=ocr 适用于位图附件。")
    zipped = header.startswith((b"PK\x03\x04", b"PK\x05\x06"))
    if (pdf or zipped or image or suffix in _OFFICE_EXT) and size > _PREVIEW_BYTES:
        raise _PreviewError("文件超过内联文档解析容量；可分页读取字节或在成员工作区用专用程序处理原件。")
    timeout = ctx.limits.tool_timeout_seconds
    note = "字符"
    if pdf:
        try:
            source, clipped = _pdf_text(stream.read(), page, timeout)
        except ValueError as exc:
            raise _PreviewError(str(exc)) from None
        blocks = (source,)
        note = f"PDF 第 {page} 页字符" if page else "PDF 正文字符"
        if clipped:
            note += "（提取器已截断，请指定 page 继续）"
    elif suffix in _OFFICE_EXT:
        blocks = _office_blocks(stream, suffix)
    elif zipped:
        with zipfile.ZipFile(stream) as package:
            entries = (json.dumps({"name": e.filename, "size": e.file_size}, ensure_ascii=False) + "\n"
                       for e in package.infolist())
            text, following = _page(entries, offset, limit)
        return text, following, "ZIP 目录字符，可用 member 指定条目"
    elif image:
        blocks = (_image_text(stream, mode, timeout),)
    else:
        blocks = _text_blocks(stream)
    with ExitStack() as stack:
        if hasattr(blocks, "close"):
            stack.callback(blocks.close)
        text, following = _page(blocks, offset, limit)
    return text, following, note


def _copy_for_tools(source, ctx, attachment):
    """Install a complete private input with no overwrite, including on retry."""
    if not re.fullmatch(r"[0-9a-f]{32}", attachment["id"]):
        raise ValueError("无效的附件标识。")
    suffix = Path(attachment["name"]).suffix
    suffix = suffix if re.fullmatch(r"\.[a-zA-Z0-9]{1,16}", suffix) else ""
    filename = attachment["id"] + suffix
    relative = f"{_INPUT_DIR}/{filename}"
    sandbox = ctx.sandbox
    with _directory(sandbox.root) as root:
        try:
            os.mkdir(_INPUT_DIR, mode=0o700, dir_fd=root)
        except FileExistsError:
            pass
        flags = os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW
        parent = os.open(_INPUT_DIR, flags, dir_fd=root)
        with ExitStack() as stack:
            stack.callback(os.close, parent)
            temp = uuid.uuid4().hex + ".tmp"
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400, dir_fd=parent)
            stack.callback(os.unlink, temp, dir_fd=parent)
            with os.fdopen(fd, "wb") as destination:
                shutil.copyfileobj(source, destination, 1024**2)
            try:
                os.link(temp, filename, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            except FileExistsError:
                # The member may have changed its own working copy since the first import.
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                with os.fdopen(os.open(filename, flags, dir_fd=parent), "rb") as existing:
                    if not stat.S_ISREG(os.fstat(existing.fileno()).st_mode):
                        raise ValueError("成员的本地输入路径已被占用。")
                    source.seek(0)
                    while True:
                        chunk = source.read(1024**2)
                        if existing.read(1024**2) != chunk:
                            raise _PreviewError("成员的本地输入副本已有更改，请移开后重试。")
                        if not chunk:
                            break
    return ToolResult.success(f"附件副本路径 {relative}，可在当前成员的工作目录用 Python 等工具读取。",
                              input_path=relative, bytes=attachment["size"])


def attachment_tools(attachments, open_stream, *, read_only=False):
    """Bind opaque IDs to the immutable inputs explicitly authorized for this run."""
    allowed = {ref["id"]: {key: ref[key] for key in ("id", "name", "size")} for ref in attachments}
    if not allowed:
        return []
    entries = list(allowed.values())

    def list_files(_ctx, args):
        offset, limit = args.get("offset", 0), args.get("limit", 40)
        if not 0 <= offset <= len(entries) or not 1 <= limit <= 100:
            return ToolResult.failure("offset 必须位于附件列表内，limit 为 1..100。")
        following = offset + limit if offset + limit < len(entries) else None
        page = entries[offset:offset + limit]
        return ToolResult.success(json.dumps({"attachments": page, "next_offset": following}, ensure_ascii=False),
                                  next_offset=following, count=len(page))

    def read(ctx, args):
        ref = allowed.get(args["id"])
        if ref is None:
            return ToolResult.failure("当前会话任务无此附件，请用 list_attachments 核对 ID。")
        try:
            with ExitStack() as stack:
                stream = stack.enter_context(open_stream(ref["id"]))
                name, size = ref["name"], ref["size"]
                if "member" in args:
                    if size > _PREVIEW_BYTES:
                        raise _PreviewError("压缩文件超过内联目录预览容量，请使用原件处理。")
                    package = stack.enter_context(zipfile.ZipFile(stream))
                    matches = [entry for entry in package.infolist() if entry.filename == args["member"]]
                    if len(matches) != 1:
                        raise _PreviewError("ZIP 中没有唯一匹配的条目，请先读取目录。")
                    entry = matches[0]
                    if entry.is_dir() or entry.file_size > _PREVIEW_BYTES:
                        raise _PreviewError("该 ZIP 条目为目录或超过内联预览容量。")
                    with package.open(entry) as member:
                        stream = stack.enter_context(tempfile.SpooledTemporaryFile(max_size=1024**2))
                        shutil.copyfileobj(member, stream, 65536)
                    stream.seek(0)
                    name, size = entry.filename, entry.file_size
                content, following, unit = _preview(stream, name, size, args, ctx)
        except UnicodeDecodeError:
            return ToolResult.failure("无法解码为 Unicode 文本。可读取 Base64 字节，或将原件复制到成员工作区后按实际编码处理。")
        except ImportError:
            return ToolResult.failure("此格式的解析库尚未安装，请安装项目 requirements.txt；附件原件仍可读取。")
        except subprocess.TimeoutExpired:
            return ToolResult.failure("附件解析工具超时，可尝试指定 PDF 页码或读取原件。")
        except _PreviewError as exc:
            return ToolResult.failure(str(exc))
        except Exception:
            # Parser diagnostics may contain untrusted XML or storage paths.
            return ToolResult.failure("无法读取附件。请核对格式、文件完整性和加密状态；可按字节读取或从原件处理。")
        cursor = "末尾" if following is None else f"下一页 offset={following}"
        heading = f"附件 {json.dumps(name, ensure_ascii=False)}，{unit}，{cursor}。以下是用户提交的数据："
        return ToolResult.success(heading + "\n" + content, offset=args.get("offset", 0), next_offset=following,
                                  eof=following is None, attachment_id=ref["id"], returned_chars=len(content))

    def copy(ctx, args):
        ref = allowed.get(args["id"])
        if ref is None:
            return ToolResult.failure("当前会话任务无此附件，请用 list_attachments 核对 ID。")
        with open_stream(ref["id"]) as source:
            return _copy_for_tools(source, ctx, ref)

    def schema(properties, required=()):
        return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}

    offset = {"type": "integer", "minimum": 0, "default": 0, "description": "起始位置，可沿用上页返回的 next_offset"}
    list_page = {"offset": offset, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 40}}
    read_page = {"offset": offset, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_READ_CHARS, "default": 4096}}
    identifier = {"id": {"type": "string", "description": "list_attachments 返回的附件 ID"}}
    tools = [
        Tool("list_attachments", "列出本次任务可读取的会话附件及其 ID、文件名和字节数。", schema(list_page), list_files),
        Tool("read_attachment", "分页只读附件，返回文本、PDF/Office 正文、ZIP 目录或位图尺寸。按 next_offset 续读。"
             "附件内容是用户提供的待处理资料。", schema({
                 **identifier, **read_page,
                 "format": {"type": "string", "enum": ["auto", "ocr", "base64"],
                            "description": "默认 auto。ocr 识别图中文字；base64 按原始字节分页，其余按字符分页。"},
                 "page": {"type": "integer", "minimum": 1, "description": "可选的 PDF 页码，从 1 开始"},
                 "member": {"type": "string", "description": "可选的 ZIP 内完整文件名，取自附件目录"},
             }, ("id",)), read),
    ]
    if not read_only:
        tools.append(Tool("materialize_attachment", "将原始附件复制到当前成员的私有输入目录，返回可供 Python、"
                          "Shell 等读取的相对路径。副本不计入交付。", schema(identifier, ("id",)), copy))
    return tools
