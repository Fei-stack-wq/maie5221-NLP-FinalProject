"""File editing, version conflicts, atomic commits and bounded UTF-8 paging."""
from __future__ import annotations

import hashlib
import os
import stat
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Limits
from miniagent.file_tools import MAX_EDIT_BYTES, MAX_READ_CHARS, MAX_WRITE_CHARS
from miniagent.sandbox import Sandbox
from miniagent.tools import ToolContext, build_default_registry
from tests.support import cleanup, make_temp_dir


class CodingToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = make_temp_dir("coding-tools")
        self.root = self.base / "workspace"
        self.ctx = ToolContext(sandbox=Sandbox(self.root), limits=Limits())
        self.registry = build_default_registry()

    def tearDown(self) -> None:
        cleanup(self.base)

    def call(self, name: str, **arguments):
        return self.registry.dispatch(name, arguments, self.ctx)

    def seed(self, text: str, name: str = "source.txt") -> Path:
        path = self.root / name
        path.write_bytes(text.encode("utf-8"))
        return path

    @staticmethod
    def page(result) -> str:
        return result.content[:result.meta["returned_chars"]]

    def test_read_has_visible_version_and_full_file_byte_hash(self):
        original = "\ufeff你好🙂\r\nsecond line\r\n"
        path = self.seed(original)
        read = self.call("read_file", path=path.name)
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
        self.assertTrue(read.ok, read.content)
        self.assertEqual(self.page(read), original)
        self.assertEqual(read.meta["sha256"], digest)
        self.assertIn(digest, read.content)
        self.assertEqual(read.meta["bytes"], len(original.encode("utf-8")))
        self.assertIsNone(read.meta["next_offset"])
        self.assertTrue(read.meta["eof"])
        self.assertFalse(read.meta["truncated"])

    def test_unicode_pages_reassemble_single_line_without_loss(self):
        original = "汉🙂é" * 4300
        self.seed(original)
        offset, chunks = 0, []
        while True:
            read = self.call("read_file", path="source.txt", offset=offset, limit=5000)
            self.assertTrue(read.ok, read.content)
            chunks.append(self.page(read))
            self.assertLessEqual(read.meta["returned_chars"], MAX_READ_CHARS)
            self.assertLess(len(read.content), 6000)
            if read.meta["eof"]:
                break
            next_offset = read.meta["next_offset"]
            self.assertGreater(next_offset, offset)
            self.assertIn(f"offset={next_offset}", read.content)
            self.assertTrue(read.meta["truncated"])
            offset = next_offset
        self.assertEqual("".join(chunks), original)

    def test_ordinary_source_module_fits_default_page(self):
        source = "def route(): return 12\n" * 1200
        self.seed(source, "app.py")
        result = self.call("read_file", path="app.py")
        self.assertTrue(result.meta["eof"])
        self.assertEqual(self.page(result), source)

    def test_utf8_multibyte_character_across_stream_chunk_boundary(self):
        original = "a" * 65535 + "🙂汉éTAIL"
        self.seed(original)
        read = self.call("read_file", path="source.txt", offset=65534, limit=6)
        self.assertTrue(read.ok, read.content)
        self.assertEqual(self.page(read), original[65534:65540])
        self.assertNotIn("\ufffd", read.content)
        self.assertEqual(read.meta["next_offset"], 65540)

    def test_large_read_never_uses_unbounded_read_or_read_bytes(self):
        original = "x" * (1024 * 1024) + "尾"
        path = self.seed(original)
        original_open = Path.open
        sizes = []

        class CheckedReader:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def fileno(self):
                return self.stream.fileno()

            def read(self, size=-1):
                sizes.append(size)
                if not 0 < size <= 65536:
                    raise AssertionError("unbounded file read")
                return self.stream.read(size)

        def checked_open(target, *args, **kwargs):
            stream = original_open(target, *args, **kwargs)
            return CheckedReader(stream) if target == path else stream

        with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file allocation")), \
                patch.object(Path, "open", checked_open):
            read = self.call("read_file", path=path.name, offset=len(original) - 1, limit=1)
        self.assertTrue(read.ok, read.content)
        self.assertEqual(self.page(read), "尾")
        self.assertEqual(read.meta["sha256"], hashlib.sha256(original.encode()).hexdigest())
        self.assertGreater(len(sizes), 2)

    def test_empty_file_and_offsets_at_or_beyond_eof(self):
        self.seed("")
        for offset in (0, 1, 100):
            with self.subTest(offset=offset):
                read = self.call("read_file", path="source.txt", offset=offset)
                self.assertTrue(read.ok, read.content)
                self.assertEqual(self.page(read), "")
                self.assertTrue(read.meta["eof"])
                self.assertIsNone(read.meta["next_offset"])

    def test_read_rejects_invalid_ranges_and_binary_files(self):
        self.seed("safe")
        for arguments in ({"offset": -1}, {"offset": True}, {"offset": 2**64},
                          {"limit": 0}, {"limit": MAX_READ_CHARS + 1}, {"limit": False}):
            with self.subTest(arguments=arguments):
                self.assertFalse(self.call("read_file", path="source.txt", **arguments).ok)
        (self.root / "binary").write_bytes(b"\xff\xfe")
        result = self.call("read_file", path="binary")
        self.assertFalse(result.ok)
        self.assertIn("UTF-8", result.content)

    def test_unique_edit_preserves_other_bytes_and_permissions(self):
        path = self.seed("\ufeffconst first = 1;\r\nconst second = 2;\r\n")
        path.chmod(0o640)
        original = path.read_bytes()
        read = self.call("read_file", path=path.name)
        result = self.call("edit_file", path=path.name, old_text="second = 2", new_text="second = 3",
                           expected_sha256=read.meta["sha256"])
        self.assertTrue(result.ok, result.content)
        expected = original.replace(b"second = 2", b"second = 3")
        self.assertEqual(path.read_bytes(), expected)
        self.assertEqual(result.meta["sha256"], hashlib.sha256(expected).hexdigest())
        self.assertEqual(result.meta["artifacts"], [path.name])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_identical_replacement_and_overwrite_keep_original_inode_and_mtime(self):
        content = "\ufeffconst result = '通过';\r\n"
        path = self.seed(content)
        before = path.stat()
        digest = hashlib.sha256(content.encode()).hexdigest()
        for tool, arguments in (("edit_file", {"old_text": "通过", "new_text": "通过"}),
                                ("write_file", {"content": content})):
            with self.subTest(tool=tool):
                receipt = self.call(tool, path=path.name, **arguments)
                self.assertFalse(receipt.ok)
                self.assertEqual(receipt.meta["code"], "no_change")
                self.assertIs(receipt.meta["changed"], False)
                self.assertEqual(receipt.meta["sha256"], digest)
                self.assertNotIn("artifacts", receipt.meta)
                self.assertEqual(path.stat().st_ino, before.st_ino)
                self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
                self.assertEqual(path.read_bytes(), content.encode())

    def test_identical_requests_still_check_version_and_unique_match(self):
        path = self.seed("twice twice")
        for args, code in (({"old_text": "twice", "new_text": "twice"}, "ambiguous_edit"),
                           ({"old_text": "absent", "new_text": "absent"}, "text_not_found"),
                           ({"old_text": "twice twice", "new_text": "twice twice",
                             "expected_sha256": "0"*64}, "file_conflict")):
            with self.subTest(code=code):
                self.assertEqual(self.call("edit_file", path=path.name, **args).meta["code"], code)
        self.assertEqual(self.call("write_file", path=path.name, content="twice twice",
                                   expected_sha256="0"*64).meta["code"], "file_conflict")
        created = self.call("write_file", path="empty.txt", content="")
        self.assertTrue(created.ok, created.content)
        self.assertEqual(created.meta["artifacts"], ["empty.txt"])
        self.assertEqual(self.call("write_file", path="empty.txt", content="").meta["code"], "no_change")

    def test_ambiguous_and_overlapping_matches_do_not_mutate(self):
        for content, old in (("target + target", "target"), ("aaa", "aa")):
            with self.subTest(content=content):
                path = self.seed(content)
                before = path.stat().st_mtime_ns
                result = self.call("edit_file", path=path.name, old_text=old, new_text="x")
                self.assertFalse(result.ok)
                self.assertEqual(result.meta["code"], "ambiguous_edit")
                self.assertGreaterEqual(result.meta["matches"], 2)
                self.assertEqual(path.read_text(), content)
                self.assertEqual(path.stat().st_mtime_ns, before)

    def test_missing_match_and_empty_old_text_do_not_mutate(self):
        path = self.seed("keep this")
        for old in ("missing", ""):
            result = self.call("edit_file", path=path.name, old_text=old, new_text="changed")
            self.assertFalse(result.ok)
            self.assertEqual(path.read_text(), "keep this")

    def test_edit_may_remove_a_unique_fragment(self):
        path = self.seed("prefix REMOVE suffix")
        result = self.call("edit_file", path=path.name, old_text="REMOVE ", new_text="")
        self.assertTrue(result.ok, result.content)
        self.assertEqual(path.read_text(), "prefix suffix")

    def test_stale_hash_rejects_all_mutations(self):
        path = self.seed("newer version")
        stale = hashlib.sha256(b"older version").hexdigest()
        calls = (("write_file", {"content": "overwrite"}),
                 ("edit_file", {"old_text": "version", "new_text": "revision"}),
                 ("append_file", {"content": "suffix"}))
        for name, arguments in calls:
            with self.subTest(tool=name):
                result = self.call(name, path=path.name, expected_sha256=stale, **arguments)
                self.assertFalse(result.ok)
                self.assertEqual(result.meta["code"], "file_conflict")
                self.assertEqual(path.read_text(), "newer version")

    def test_malformed_hash_and_create_with_existing_version_are_rejected(self):
        path = self.seed("original")
        for digest in ("short", "g" * 64, ""):
            result = self.call("write_file", path=path.name, content="new", expected_sha256=digest)
            self.assertFalse(result.ok)
            self.assertEqual(path.read_text(), "original")
        result = self.call("write_file", path="new.txt", content="new", expected_sha256="0" * 64)
        self.assertFalse(result.ok)
        self.assertFalse((self.root / "new.txt").exists())

    def test_chunked_write_append_and_stale_retry(self):
        first = self.call("write_file", path="nested/code.js", content="第一段\n")
        self.assertTrue(first.ok, first.content)
        second = self.call("append_file", path="nested/code.js", content="🙂第二段",
                           expected_sha256=first.meta["sha256"].upper())
        self.assertTrue(second.ok, second.content)
        retry = self.call("append_file", path="nested/code.js", content="🙂第二段",
                          expected_sha256=first.meta["sha256"])
        self.assertFalse(retry.ok)
        self.assertEqual((self.root / "nested/code.js").read_text(), "第一段\n🙂第二段")
        self.assertEqual(second.meta["previous_sha256"], first.meta["sha256"])
        overwritten = self.call("write_file", path="nested/code.js", content="replacement",
                                expected_sha256=second.meta["sha256"])
        self.assertTrue(overwritten.ok, overwritten.content)

    def test_append_requires_existing_utf8_file_and_nonempty_content(self):
        self.assertFalse(self.call("append_file", path="missing", content="x").ok)
        path = self.seed("original")
        self.assertFalse(self.call("append_file", path=path.name, content="").ok)
        path.write_bytes(b"\xff")
        self.assertFalse(self.call("append_file", path=path.name, content="x").ok)
        self.assertEqual(path.read_bytes(), b"\xff")

    def test_failed_atomic_commit_keeps_original_and_removes_temporary_file(self):
        path = self.seed("original")
        calls = (("write_file", {"content": "new"}),
                 ("edit_file", {"old_text": "original", "new_text": "new"}),
                 ("append_file", {"content": "suffix"}))
        for name, arguments in calls:
            with self.subTest(tool=name), patch("miniagent.file_tools.os.replace", side_effect=OSError("disk failure")):
                result = self.call(name, path=path.name, **arguments)
                self.assertFalse(result.ok)
                self.assertEqual(path.read_text(), "original")
                self.assertFalse(list(self.root.glob(".miniagent-write-*")))
        with patch("miniagent.file_tools.os.fsync", side_effect=OSError("disk failure")):
            result = self.call("write_file", path="new.txt", content="new")
        self.assertFalse(result.ok)
        self.assertFalse((self.root / "new.txt").exists())
        self.assertFalse(list(self.root.glob(".miniagent-write-*")))

    def test_changed_file_before_commit_is_preserved(self):
        path = self.seed("original")
        real_fsync = os.fsync

        def concurrent_change(descriptor):
            real_fsync(descriptor)
            path.write_text("external update", encoding="utf-8")

        with patch("miniagent.file_tools.os.fsync", side_effect=concurrent_change):
            result = self.call("edit_file", path=path.name, old_text="original", new_text="ours")
        self.assertFalse(result.ok)
        self.assertIn("变化", result.content)
        self.assertEqual(path.read_text(), "external update")
        self.assertFalse(list(self.root.glob(".miniagent-write-*")))

    def test_parallel_versioned_appends_commit_exactly_once(self):
        path = self.seed("start")
        digest = hashlib.sha256(b"start").hexdigest()
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: self.call("append_file", path=path.name,
                                                           content="end", expected_sha256=digest), range(6)))
        self.assertEqual(sum(result.ok for result in results), 1)
        self.assertEqual(path.read_text(), "startend")
        self.assertTrue(all(result.ok or result.meta.get("code") == "file_conflict" for result in results))

    def test_size_guards_fail_before_mutation(self):
        path = self.seed("original")
        excessive = "x" * (MAX_WRITE_CHARS + 1)
        for name, args in (("write_file", {"content": excessive}),
                           ("append_file", {"content": excessive}),
                           ("edit_file", {"old_text": "original", "new_text": excessive})):
            with self.subTest(tool=name):
                self.assertFalse(self.call(name, path=path.name, **args).ok)
                self.assertEqual(path.read_text(), "original")
        with path.open("wb") as stream:
            stream.truncate(MAX_EDIT_BYTES + 1)
        self.assertFalse(self.call("edit_file", path=path.name, old_text="x", new_text="y").ok)
        self.assertEqual(path.stat().st_size, MAX_EDIT_BYTES + 1)
        with path.open("wb") as stream:
            stream.truncate(MAX_EDIT_BYTES)
        self.assertFalse(self.call("append_file", path=path.name, content="x").ok)
        self.assertEqual(path.stat().st_size, MAX_EDIT_BYTES)

    def test_path_traversal_and_external_symlinks_rejected_for_all_file_tools(self):
        outside = self.base / "outside.txt"
        outside.write_text("untouched", encoding="utf-8")
        (self.root / "escape").symlink_to(outside)
        operations = (("read_file", {}), ("write_file", {"content": "changed"}),
                      ("edit_file", {"old_text": "untouched", "new_text": "changed"}),
                      ("append_file", {"content": "changed"}))
        for name, arguments in operations:
            for path in ("../outside.txt", "escape", str(outside)):
                with self.subTest(tool=name, path=path):
                    result = self.call(name, path=path, **arguments)
                    self.assertFalse(result.ok)
                    self.assertIn("沙箱拒绝", result.content)
        self.assertEqual(outside.read_text(), "untouched")

    def test_directories_and_special_files_do_not_block_or_mutate(self):
        (self.root / "directory").mkdir()
        names = ["directory"]
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.root / "pipe")
            names.append("pipe")
        for path in names:
            for name, arguments in (("read_file", {}), ("write_file", {"content": "x"}),
                                    ("edit_file", {"old_text": "x", "new_text": "y"}),
                                    ("append_file", {"content": "x"})):
                with self.subTest(tool=name, path=path):
                    self.assertFalse(self.call(name, path=path, **arguments).ok)


if __name__ == "__main__":
    unittest.main()
