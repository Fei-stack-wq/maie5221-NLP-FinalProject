"""工具层测试：参数校验、错误吸收、HITL 拦截、SSRF 防护。"""

from __future__ import annotations

import unittest
from pathlib import Path

from miniagent.config import Limits
from miniagent.sandbox import Sandbox
from miniagent.tools import ToolContext, build_default_registry, validate_arguments
from tests.support import cleanup, make_temp_dir


class ToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._base = make_temp_dir("tools")
        self.root = self._base / "worker"
        self.ctx = ToolContext(sandbox=Sandbox(self.root), limits=Limits())
        self.registry = build_default_registry()

    def tearDown(self) -> None:
        cleanup(self._base)

    # ------------------------------------------------------------ 参数校验
    def test_validation_reports_missing_required(self) -> None:
        errors = validate_arguments({"required": ["path"]}, {})
        self.assertIn("缺少必填参数 path", errors)

    def test_validation_reports_unknown_argument(self) -> None:
        schema = {"properties": {"path": {"type": "string"}}, "required": ["path"]}
        errors = validate_arguments(schema, {"path": "a", "hallucinated": 1})
        self.assertTrue(any("未知参数" in error for error in errors))

    def test_validation_rejects_bool_as_integer(self) -> None:
        schema = {"properties": {"n": {"type": "integer"}}}
        self.assertTrue(validate_arguments(schema, {"n": True}))

    # ------------------------------------------------------------ 文件工具
    def test_write_then_read(self) -> None:
        write = self.registry.dispatch("write_file", {"path": "a.md", "content": "你好"}, self.ctx)
        self.assertTrue(write.ok, write.content)
        self.assertTrue((self.root / "a.md").exists())

        read = self.registry.dispatch("read_file", {"path": "a.md"}, self.ctx)
        self.assertTrue(read.ok)
        self.assertEqual(read.content, "你好")

    def test_missing_required_argument_is_a_result_not_an_exception(self) -> None:
        result = self.registry.dispatch("write_file", {"path": "a.md"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("缺少必填参数", result.content)

    def test_read_missing_file(self) -> None:
        result = self.registry.dispatch("read_file", {"path": "nope.md"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("沙箱拒绝", result.content)

    def test_unknown_tool(self) -> None:
        result = self.registry.dispatch("rm_rf", {"path": "/"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("未知工具", result.content)

    def test_escape_attempt_through_tool(self) -> None:
        result = self.registry.dispatch("write_file", {"path": "../evil.md", "content": "x"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("沙箱拒绝", result.content)
        self.assertFalse((self._base / "evil.md").exists())

    # ------------------------------------------------------------ HITL
    def test_delete_is_blocked_without_approval(self) -> None:
        (self.root / "a.md").write_text("x", encoding="utf-8")
        result = self.registry.dispatch("delete_file", {"path": "a.md"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("需要人类批准", result.content)
        self.assertTrue((self.root / "a.md").exists())

    def test_delete_passes_with_approval(self) -> None:
        (self.root / "a.md").write_text("x", encoding="utf-8")
        ctx = ToolContext(
            sandbox=self.ctx.sandbox,
            limits=self.ctx.limits,
            approve=lambda prompt: True,
        )
        result = self.registry.dispatch("delete_file", {"path": "a.md"}, ctx)
        self.assertTrue(result.ok, result.content)
        self.assertFalse((self.root / "a.md").exists())

    # ------------------------------------------------------------ 代码执行
    def test_run_python(self) -> None:
        result = self.registry.dispatch("run_python", {"code": "print(2 ** 10)"}, self.ctx)
        self.assertTrue(result.ok, result.content)
        self.assertIn("1024", result.content)

    def test_run_python_failure_is_reported(self) -> None:
        result = self.registry.dispatch("run_python", {"code": "raise ValueError('boom')"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("boom", result.content)

    # ------------------------------------------------------------ 网络
    def test_http_get_blocks_localhost(self) -> None:
        result = self.registry.dispatch("http_get", {"url": "http://localhost:8000/admin"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("禁止访问内网", result.content)

    # ------------------------------------------------------------ 清单
    def test_registry_exposes_six_tools(self) -> None:
        self.assertEqual(
            self.registry.names(),
            ["delete_file", "http_get", "list_dir", "read_file", "run_python", "write_file"],
        )
        for spec in self.registry.specs():
            self.assertEqual(spec.to_api()["type"], "function")


if __name__ == "__main__":
    unittest.main()
