"""Use the actual editor tool registry and the shipped ripgrep CLI on a project."""
import tempfile
import unittest
from pathlib import Path

from miniagent.config import Limits
from miniagent.sandbox import Sandbox
from miniagent.tools import ToolContext, build_default_registry


class NavigationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "project"
        self.ctx = ToolContext(Sandbox(self.root), Limits())
        self.tools = build_default_registry(read_only=True)
        (self.root / "src").mkdir()
        (self.root / "src/routes.py").write_text("start\ndef dashboard():\n    return '预约'\n")
        (self.root / "server.py").write_text("dashboard = True\n")
        (self.root / "other.txt").write_text("DASHboard\n")
        (self.root / ".env").write_text("dashboard=hidden")
        (self.root / "ignored").mkdir()
        (self.root / "ignored/data.py").write_text("dashboard\n")
        (self.root / ".gitignore").write_text("ignored/\n")
        (self.base / "outside.py").write_text("dashboard=outside")
        (self.root / "link.py").symlink_to(self.base / "outside.py")

    def call(self, name, **args):
        return self.tools.dispatch(name, args, self.ctx)

    def test_search_context_filters_pagination_and_errors(self):
        a = self.call("search_files", query="dashboard", limit=1)
        b = self.call("search_files", query="dashboard", offset=a.meta["next_offset"])
        self.assertIn("server.py:1:dashboard", a.content)
        self.assertIn("src/routes.py:2:def dashboard", b.content)
        self.assertIsNone(b.meta["next_offset"])
        txt = self.call("search_files", query="dashBOARD", ignore_case=True, glob="*.txt")
        self.assertIn("other.txt:1", txt.content)
        source = self.call("search_files", path="src", query="dashboard()", literal=True, context=1)
        self.assertIn("预约", source.content)
        self.assertFalse(self.call("search_files", query="[").ok)
        self.assertTrue(self.call("search_files", query="nothing-here").ok)
        self.assertFalse(self.call("search_files", query="dashboard", path=".env").ok)
        self.assertFalse(self.call("search_files", query="dashboard", path="link.py").ok)

    def test_recursive_find_and_targeted_source_read(self):
        listing = self.call("find_files", pattern="**/*.py")
        self.assertEqual(listing.content.splitlines(), ["server.py", "src/routes.py"])
        first = self.call("read_lines", path="src/routes.py", start_line=2, limit=1)
        self.assertIn("2  def dashboard():", first.content)
        self.assertEqual(first.meta["next_line"], 3)
        next_page = self.call("read_lines", path="src/routes.py", start_line=3)
        self.assertIn("预约", next_page.content)
        self.assertEqual(next_page.meta["sha256"], first.meta["sha256"])
        self.assertFalse(self.call("read_lines", path="../outside.py").ok)
        (self.root / "binary.db").write_bytes(b"\xff\x00")
        self.assertFalse(self.call("read_lines", path="binary.db").ok)

    def test_directory_pages_are_stable_and_dangling_links_are_listable(self):
        (self.root / "lost").symlink_to(self.base / "missing")
        pages = []
        offset = 0
        while offset is not None:
            page = self.call("list_dir", limit=2, offset=offset)
            self.assertTrue(page.ok, page.content)
            pages.extend(line for line in page.content.splitlines() if "offset=" not in line)
            offset = page.meta["next_offset"]
        self.assertIn("[link] lost", pages)
        self.assertEqual(len(pages), len(set(pages)))
        self.assertEqual(len(pages), page.meta["total"])
