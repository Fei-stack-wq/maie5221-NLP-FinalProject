"""沙箱测试：这是隔离性的证据，也是最容易出安全洞的地方。"""

from __future__ import annotations

import unittest
from pathlib import Path

from miniagent.sandbox import Sandbox, SandboxError
from tests.support import cleanup, make_temp_dir


class SandboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self._base = make_temp_dir("sandbox")
        self.root = self._base / "agents" / "worker"
        self.sandbox = Sandbox(self.root, agent_name="worker")

    def tearDown(self) -> None:
        cleanup(self._base)

    def test_creates_root(self) -> None:
        self.assertTrue(self.root.is_dir())

    def test_relative_path_stays_inside(self) -> None:
        target = self.sandbox.resolve("notes/a.md")
        self.assertTrue(str(target).startswith(str(self.root.resolve())))

    def test_dot_path_is_root(self) -> None:
        self.assertEqual(self.sandbox.resolve("."), self.root)

    def test_rejects_parent_traversal(self) -> None:
        for bad in ("../escape.md", "a/../../escape.md", "..\\..\\Windows\\win.ini", "./../x"):
            with self.subTest(bad=bad), self.assertRaises(SandboxError):
                self.sandbox.resolve(bad)

    def test_rejects_absolute_paths(self) -> None:
        for bad in ("/etc/passwd", "C:\\Windows\\win.ini", "C:temp\\x.md", "\\\\server\\share"):
            with self.subTest(bad=bad), self.assertRaises(SandboxError):
                self.sandbox.resolve(bad)

    def test_rejects_empty(self) -> None:
        for bad in ("", "   ", None):
            with self.subTest(bad=bad), self.assertRaises(SandboxError):
                self.sandbox.resolve(bad)  # type: ignore[arg-type]

    def test_must_exist(self) -> None:
        with self.assertRaises(SandboxError):
            self.sandbox.resolve("nope.md", must_exist=True)

    def test_symlink_escape_is_rejected(self) -> None:
        """指向沙箱外的符号链接必须被拦住（Windows 上需要开发者模式，失败则跳过）。"""
        outside = self._base / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "link.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("当前系统不允许创建符号链接")
        with self.assertRaises(SandboxError):
            self.sandbox.resolve("link.txt")

    def test_files_listing_is_relative(self) -> None:
        (self.root / "a.md").write_text("hi", encoding="utf-8")
        entries = self.sandbox.files()
        self.assertEqual([entry["path"] for entry in entries], ["a.md"])


if __name__ == "__main__":
    unittest.main()
