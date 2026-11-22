"""Studio may load local credentials, but must never evaluate configuration."""
from __future__ import annotations

import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Limits, Settings
from miniagent.cli import main
from miniagent.studio_config import load_studio_settings
from tests.support import cleanup, make_temp_dir


class StudioConfigTest(unittest.TestCase):
    def test_zero_in_environment_or_file_removes_only_the_cumulative_cap(self):
        self.path.write_text("MA_MAX_TOTAL_TOKENS=0\n")
        base = Settings(limits=Limits(max_total_tokens=2500, max_steps=7))
        with patch.dict(os.environ, {}, clear=True):
            loaded = load_studio_settings(base, self.path)
        self.assertEqual((loaded.limits.max_total_tokens, loaded.limits.max_steps), (0, 7))
        self.assertEqual(base.limits.max_total_tokens, 2500)
        self.path.write_text("MA_MAX_TOTAL_TOKENS=1000000\n")
        with patch.dict(os.environ, {"MA_MAX_TOTAL_TOKENS": "0"}, clear=True):
            loaded = load_studio_settings(Settings.from_env(), self.path)
        self.assertEqual(loaded.limits.max_total_tokens, 0)

    def test_nested_budgets_load_and_environment_wins_without_resetting_other_limits(self):
        self.path.write_text("MA_MAX_STEPS=250\nMA_MAX_TOTAL_TOKENS=4000000\nMA_MAX_SECONDS=7200\n"
                             "MA_OUTPUT_RECOVERY_ATTEMPTS=1\nMA_CONTEXT_WINDOW_TOKENS=900000\n")
        with patch.dict(os.environ, {"MA_MAX_STEPS": "175"}, clear=True):
            original = Settings.from_env()
            original.limits.tool_timeout_seconds = 17
            value = load_studio_settings(original, self.path)
        self.assertEqual((value.limits.max_steps, value.limits.max_total_tokens, value.limits.max_seconds),
                         (175, 4000000, 7200))
        self.assertEqual(value.limits.tool_timeout_seconds, 17)
        self.assertEqual(original.limits.max_total_tokens, 0)
        self.assertEqual(value.max_output_recovery_attempts, 1)
        self.assertEqual(value.context_window_tokens, 900000)

    def setUp(self):
        self.root = make_temp_dir("studio-config")
        self.path = self.root / "sample.env"

    def tearDown(self):
        cleanup(self.root)

    def test_literal_values_quotes_comments_and_export_preserve_base_settings(self):
        self.path.write_text('export DEEPSEEK_API_KEY="synthetic-local-key" # local only\n'
                             'DEEPSEEK_THINKING=enabled\nDEEPSEEK_MAX_TOKENS=2048\n'
                             'UNRELATED_CONFIGURATION=ignored\n')
        original = Settings(thinking="disabled")
        with patch.dict(os.environ, {}, clear=True):
            value = load_studio_settings(original, self.path)
        self.assertEqual(value.api_key, "synthetic-local-key")
        self.assertEqual(value.thinking, "enabled")
        self.assertEqual(value.max_output_tokens, 2048)
        self.assertEqual(original.api_key, "")
        self.assertEqual(original.thinking, "disabled")

    def test_existing_environment_takes_precedence_without_mutating_environment(self):
        self.path.write_text('DEEPSEEK_API_KEY=file-key\nDEEPSEEK_MAX_TOKENS=1024\n')
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "environment-key"}, clear=True):
            before = dict(os.environ)
            value = load_studio_settings(Settings.from_env(), self.path)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(value.api_key, "environment-key")
        self.assertEqual(value.max_output_tokens, 1024)

    def test_shell_expressions_are_rejected_without_execution_or_value_echo(self):
        marker = self.root / "must-not-exist"
        values = [f"$(touch {marker})", f"`touch {marker}`", "${PRIVATE_SECRET}"]
        for source in values:
            self.path.write_text(f'DEEPSEEK_API_KEY="{source}"\n')
            with self.subTest(source=source), patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError) as error:
                    load_studio_settings(Settings(), self.path)
                self.assertNotIn(source, str(error.exception))
                self.assertFalse(marker.exists())

    def test_invalid_numeric_configuration_does_not_echo_secret(self):
        self.path.write_text('DEEPSEEK_API_KEY=synthetic-private-key\nDEEPSEEK_MAX_TOKENS=bad\n')
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError) as error:
            load_studio_settings(Settings(), self.path)
        self.assertNotIn("synthetic-private-key", str(error.exception))
        self.assertNotIn("bad", str(error.exception))

    def test_missing_explicit_file_fails_and_optional_default_does_not(self):
        with self.assertRaises(FileNotFoundError):
            load_studio_settings(Settings(), self.path)
        with patch.object(Path, "exists", return_value=False):
            base = Settings()
            self.assertIs(load_studio_settings(base), base)

    def test_invalid_encoding_and_oversize_files_are_rejected(self):
        for value in [b"\xff\xfe", b"#" * 65_537]:
            with self.subTest(size=len(value)):
                self.path.write_bytes(value)
                with self.assertRaises(ValueError):
                    load_studio_settings(Settings(), self.path)

    def test_studio_cli_loads_selected_config_only_on_server_side(self):
        self.path.write_text("DEEPSEEK_API_KEY=synthetic-local-key\nDEEPSEEK_THINKING=enabled\n")
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with patch("miniagent.studio_server.serve") as serve:
                code = main(["studio", "--env-file", str(self.path), "--runs-dir", str(self.root / "runs"), "--port", "8768"])
        self.assertEqual(code, 0)
        self.assertEqual(serve.call_args.args[0].settings.api_key, "synthetic-local-key")
        self.assertEqual(serve.call_args.args[0].settings.thinking, "enabled")
        self.assertEqual(serve.call_args.kwargs["port"], 8768)
        self.assertNotIn("synthetic-local-key", output.getvalue())

    def test_studio_cli_rejects_conflicting_config_and_invalid_port_before_serving(self):
        for args in [["studio", "--no-env", "--env-file", str(self.path)], ["studio", "--port", "0"]]:
            with self.subTest(args=args), patch.dict(os.environ, {}, clear=True):
                with patch("miniagent.studio_server.serve") as serve, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(args), 2)
                    serve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
