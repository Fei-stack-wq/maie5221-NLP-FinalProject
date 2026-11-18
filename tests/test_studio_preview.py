"""HTML previews load only explicitly linked project assets and cannot use Studio privileges."""
import base64
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from miniagent.config import Settings
from miniagent.studio_preview import PREVIEW_CSP, html_preview
from miniagent.studio_service import StudioError, StudioService


class StudioPreviewTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.service = StudioService(Path(scratch.name), Settings())
        self.addCleanup(self.service.close)
        self.files = Path(scratch.name) / "run/agents/worker/presentation"
        self.files.mkdir(parents=True)

    def render(self, content):
        (self.files / "slides.html").write_text(content, encoding="utf-8")
        return html_preview(self.service, "run", "agents/worker/presentation/slides.html").decode()

    def test_local_dependencies_and_isolated_frame(self):
        (self.files / "deck.js").write_text("const title = '行业';", encoding="utf-8")
        (self.files / "deck.css").write_text("body{color:navy}")
        page = self.render('<link rel="stylesheet" href="deck.css"><script src="deck.js?v=2"></script>')
        encoded = re.search(r'src="data:text/html;charset=utf-8;base64,([^"]*)"', page)[1]
        child = base64.b64decode(encoded).decode()
        self.assertIn('src="data:text/javascript;base64,', child)
        self.assertIn('href="data:text/css;base64,', child)
        self.assertIn('sandbox="allow-scripts"', page)
        self.assertIn("connect-src 'none'", PREVIEW_CSP)
        self.assertIn("frame-ancestors 'none'", PREVIEW_CSP)
        self.assertNotIn("<script", page)

    def test_missing_remote_or_out_of_workspace_script_is_rejected(self):
        for path in ("not-present.js", "https://example.invalid/library.js", "../another-user.js", "/api/bootstrap"):
            with self.subTest(path=path), self.assertRaises(StudioError):
                self.render('<script src="' + path + '"></script>')

    def test_truncated_html_and_dependencies_are_never_half_rendered(self):
        with self.assertRaisesRegex(StudioError, "上限"):
            self.render("x" * 300000)
        (self.files / "big.js").write_text("x" * 300000)
        with self.assertRaisesRegex(StudioError, "上限"):
            self.render('<script src="big.js"></script>')
