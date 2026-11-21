"""Conversation-scoped inputs, real document parsers and private workspace copies."""
import base64
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from miniagent.attachment_tools import attachment_tools
from miniagent.config import Limits
from miniagent.sandbox import Sandbox
from miniagent.studio_conversations import copy_project
from miniagent.tools import ToolContext, ToolRegistry


class AttachmentToolsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.sandbox = Sandbox(self.base / "agents/alice")
        self.ctx = ToolContext(self.sandbox, Limits())
        self.identity = "a" * 32

    def registry(self, name, data, *, read_only=False):
        ref = {"id": self.identity, "name": name, "size": len(data)}
        def open_stream(identity):
            self.assertEqual(identity, self.identity)
            return io.BytesIO(data)
        return ToolRegistry(attachment_tools([ref], open_stream, read_only=read_only))

    def read(self, registry, **args):
        return registry.dispatch("read_attachment", {"id": self.identity, **args}, self.ctx)

    def test_unicode_text_cursor_and_read_only_scope(self):
        reg = self.registry("笔记.txt", "上🙂下".encode("utf-16"), read_only=True)
        self.assertNotIn("materialize_attachment", reg.names())
        listed = json.loads(reg.dispatch("list_attachments", {}, self.ctx).content)
        self.assertEqual(listed["attachments"][0]["id"], self.identity)
        first = self.read(reg, limit=2)
        self.assertTrue(first.ok, first.content)
        self.assertTrue(first.content.endswith("上🙂"))
        last = self.read(reg, offset=first.meta["next_offset"])
        self.assertTrue(last.content.endswith("下"))
        self.assertTrue(last.meta["eof"])
        self.assertFalse(self.read(reg, id="b" * 32).ok)
        self.assertEqual(list(self.sandbox.root.iterdir()), [])

    def test_zip_member_is_only_read_as_data_even_if_it_has_a_path(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as package:
            package.writestr("../../outside.txt", "精确内容")
        reg = self.registry("资料.zip", stream.getvalue())
        self.assertIn("../../outside.txt", self.read(reg).content)
        member = self.read(reg, member="../../outside.txt")
        self.assertTrue(member.ok, member.content)
        self.assertTrue(member.content.endswith("精确内容"))
        self.assertFalse(self.read(reg, member="missing.txt").ok)
        self.assertFalse((self.base / "outside.txt").exists())
        self.assertEqual(list(self.sandbox.root.iterdir()), [])

    def test_materialization_never_overwrites_or_publishes_input(self):
        data = b"\x00\x01\xff\x02"
        registry = self.registry("data.bin", data)
        binary = self.read(registry, format="base64", offset=1, limit=2)
        self.assertEqual(binary.meta["next_offset"], 3)
        self.assertTrue(binary.content.endswith(base64.b64encode(data[1:3]).decode()))
        args = {"id": self.identity}
        copy = registry.dispatch("materialize_attachment", args, self.ctx)
        self.assertTrue(copy.ok, copy.content)
        path = self.sandbox.resolve(copy.meta["input_path"], must_exist=True)
        self.assertEqual(path.read_bytes(), data)
        self.assertTrue(registry.dispatch("materialize_attachment", args, self.ctx).ok)
        manifest = {}
        copy_project(self.base, "alice", None, {".txt"}, manifest=manifest)
        self.assertEqual(manifest, {})
        path.chmod(0o600)
        path.write_bytes(b"member edited")
        self.assertFalse(registry.dispatch("materialize_attachment", args, self.ctx).ok)
        self.assertEqual(path.read_bytes(), b"member edited")

    def test_private_input_dir_symlink_fails_closed(self):
        outside = self.base / "elsewhere"
        outside.mkdir()
        (self.sandbox.root / ".studio-inputs").symlink_to(outside, target_is_directory=True)
        reg = self.registry("data.bin", b"\0")
        self.assertFalse(reg.dispatch("materialize_attachment", {"id": self.identity}, self.ctx).ok)
        self.assertEqual(list(outside.iterdir()), [])

    def test_office_text_tables_and_slides(self):
        from docx import Document
        from openpyxl import Workbook
        from pptx import Presentation
        from pptx.util import Inches

        doc = Document()
        doc.add_paragraph("正文")
        doc.add_table(rows=1, cols=1).cell(0, 0).text = "表格"
        book = Workbook()
        book.active.title = "销售"
        book.active.append(["季度", 2026])
        slides = Presentation()
        slide = slides.slides.add_slide(slides.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(2), Inches(1)).text = "回顾"
        for name, obj, snippets in [("word.docx", doc, ["正文", "表格"]),
                                    ("表.xlsx", book, ["销售", "季度\t2026"]),
                                    ("简报.pptx", slides, ["幻灯片 1", "回顾"])]:
            with self.subTest(name=name):
                stream = io.BytesIO()
                obj.save(stream)
                result = self.read(self.registry(name, stream.getvalue()))
                self.assertTrue(result.ok, result.content)
                for snippet in snippets:
                    self.assertIn(snippet, result.content)
        book.close()

    def test_image_metadata_does_not_require_ocr_and_pdf_page_is_forwarded(self):
        stream = io.BytesIO()
        with Image.new("RGB", (12, 8)) as image:
            image.save(stream, format="PNG")
        self.assertIn("12 × 8", self.read(self.registry("图.png", stream.getvalue())).content)
        with patch("miniagent.attachment_tools._pdf_text", return_value=("第2页原文", False)) as pdf_text:
            data = b"%PDF-1.7\n%%EOF"
            result = self.read(self.registry("资料.pdf", data), page=2)
            self.assertTrue(result.content.endswith("第2页原文"))
            pdf_text.assert_called_once_with(data, 2, self.ctx.limits.tool_timeout_seconds)
