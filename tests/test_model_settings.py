"""Studio-wide model choice from 模型连接: saved to .env, shown in bootstrap, used by the next task."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from miniagent.server import serve
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_server import request
from tests.test_team import TEAM, finished


class ModelSettingsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.created = []
        self.main = FakeProvider(Script(
            lead=[reply("", call("delegate_task", assignee="qa", task="Review notes.md for mistakes.", review=True)),
                  reply("done")]))
        self.reviewer = FakeProvider(Script(qa=[reply("VERDICT: APPROVED")]))

        def factory(s):
            self.created.append((s.model, s.thinking))
            return self.reviewer if s.model == "deepseek-v4-pro" else self.main

        self.studio = Studio(settings(root), factory)
        self.studio.env_file = root / ".env"
        self.studio.env_file.write_text("DEEPSEEK_API_KEY=fake-local-key\nMA_TEMPERATURE=0\n")
        self.server, self.preview = serve(self.studio, 0)
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.addCleanup(self._stop)

    def _stop(self):
        for server in (self.server, self.preview):
            server.shutdown()
            server.server_close()
        self.studio.close()

    def post(self, body):
        status, _, data = request(self.base + "/api/settings/models", "POST", body)
        return status, json.loads(data)

    def test_saving_models_updates_env_bootstrap_and_the_next_task(self):
        boot = json.loads(request(self.base + "/api/bootstrap")[2])
        self.assertEqual((boot["model"], boot["reviewer_model"], boot["reviewer_thinking"]), ("deepseek-flash", "", ""))
        self.assertEqual(boot["models"], ["deepseek-flash", "deepseek-v4-pro"])

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MA_REVIEWER_MODEL", None)
            status, result = self.post({"model": "deepseek-flash", "reviewer_model": "deepseek-v4-pro",
                                        "reviewer_thinking": "disabled"})
        self.assertEqual(status, 200)
        self.assertEqual((result["reviewer_model"], result["reviewer_thinking"], result["environment_override"]),
                         ("deepseek-v4-pro", "disabled", []))
        env = self.studio.env_file.read_text()
        self.assertIn("MA_REVIEWER_MODEL=deepseek-v4-pro\n", env)
        self.assertIn("MA_REVIEWER_THINKING=disabled\n", env)
        self.assertIn("DEEPSEEK_API_KEY=fake-local-key\n", env, "other settings are kept")
        self.assertEqual(self.studio.env_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("fake-local-key", json.dumps(result))
        boot = json.loads(request(self.base + "/api/bootstrap")[2])
        self.assertEqual((boot["reviewer_model"], boot["reviewer_thinking"]), ("deepseek-v4-pro", "disabled"))

        gid = self.studio.create_group("Team", TEAM, "lead", "autonomous")["id"]
        self.studio.post(gid, "Check the notes", queue=True)
        task = finished(self.studio, gid)
        self.assertEqual(task["status"], "completed")
        self.assertIn(("deepseek-v4-pro", "disabled"), self.created)
        self.assertEqual([r["agent"] for r in self.reviewer.requests], ["qa"], "the review ran on the reviewer model")

        status, result = self.post({"reviewer_model": "", "reviewer_thinking": ""})
        self.assertEqual((status, result["reviewer_model"], result["reviewer_thinking"]), (200, "", ""))
        self.assertNotIn("MA_REVIEWER", self.studio.env_file.read_text(), "back to the authors' settings")

    def test_invalid_model_settings_are_refused_and_nothing_is_written(self):
        before = self.studio.env_file.read_text()
        for body in ({"reviewer_model": "gpt-4"}, {"reviewer_thinking": "maybe"}, {"temperature": "1"}, {}):
            status, result = self.post(body)
            self.assertEqual(status, 400, body)
        self.assertEqual(self.studio.env_file.read_text(), before)
        self.assertEqual(self.studio.settings.reviewer_model, "")

    def test_environment_override_is_reported(self):
        with mock.patch.dict(os.environ, {"MA_REVIEWER_THINKING": "enabled"}):
            status, result = self.post({"reviewer_thinking": "disabled"})
        self.assertEqual((status, result["environment_override"]), (200, ["MA_REVIEWER_THINKING"]))


if __name__ == "__main__":
    unittest.main()
