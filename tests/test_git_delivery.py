import hashlib
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from miniagent.config import Limits
from miniagent.sandbox import Sandbox
from miniagent.tools import ToolContext, build_default_registry


@unittest.skipUnless(shutil.which("git"), "Git executable required")
class GitDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.remote, self.project = root / "remote.git", root / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "--bare", "--quiet", str(self.remote)], check=True)
        self.source = self.project / "report.md"
        self.source.write_text("reviewable version\n", encoding="utf-8")
        self.registry = build_default_registry(git_test_remote=str(self.remote), secret="sentinel-secret-key")

    def refs(self):
        return subprocess.run(["git", "--git-dir=" + str(self.remote), "show-ref"], capture_output=True, text=True).stdout

    def publish(self, approve, **args):
        ctx = ToolContext(Sandbox(self.project), Limits(tool_timeout_seconds=10), approve=approve)
        return self.registry.dispatch("git_publish", {"branch": "nlp-test/fixture", "files": ["report.md"], **args}, ctx)

    def test_unconfigured_or_readonly_registry_does_not_offer_git(self):
        self.assertNotIn("git_publish", build_default_registry().names())
        self.assertNotIn("git_publish", build_default_registry(read_only=True, git_test_remote=str(self.remote)).names())

    def test_denial_and_missing_approval_never_write_remote(self):
        for approval in (None, lambda _: False):
            result = self.publish(approval)
            self.assertFalse(result.ok)
            self.assertEqual(result.meta["decision"], "deny")
            self.assertEqual(self.refs(), "")

    def test_approved_immutable_commit_is_verified_and_existing_branch_rejected(self):
        expected = self.source.read_bytes()
        prompts = []
        def approve(prompt):
            prompts.append(prompt)
            self.source.write_text("changed while approval was pending", encoding="utf-8")
            return True
        result = self.publish(approve)
        self.assertTrue(result.ok, result)
        self.assertIn(str(self.remote), prompts[0])
        self.assertIn(hashlib.sha256(expected).hexdigest(), prompts[0])
        actual = subprocess.check_output(["git", "--git-dir=" + str(self.remote), "show", "refs/heads/nlp-test/fixture:report.md"])
        self.assertEqual(actual, expected)
        self.assertIn(result.meta["commit"], self.refs())
        before = self.refs()
        self.assertFalse(self.publish(lambda _: self.fail("existing branch must not ask again")).ok)
        self.assertEqual(self.refs(), before)

    def test_unsafe_paths_targets_and_credentials_fail_before_approval(self):
        for change in ({"files": ["../report.md"]}, {"files": [".env"]}, {"branch": "main"}):
            self.assertFalse(self.publish(lambda _: self.fail("invalid request"), **change).ok)
        self.source.write_text("sentinel-secret-key", encoding="utf-8")
        self.assertFalse(self.publish(lambda _: self.fail("secret must not be published")).ok)
        self.assertEqual(self.refs(), "")
        for remote in ("https://github.com/a/b?token=secret", "https://evil.example/a/b", "-malicious"):
            with self.assertRaises(ValueError):
                build_default_registry(git_test_remote=remote)
