"""Offline configuration contracts; these tests never need credentials or HTTP."""
from __future__ import annotations

import os
import traceback
import unittest
from unittest.mock import patch

from miniagent.config import Limits, Settings, configured_limits, validate_provider_options


class SettingsTest(unittest.TestCase):
    def test_defaults_work_without_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.model, "deepseek-flash")
        self.assertEqual(settings.base_url, "https://api.deepseek.com")
        self.assertEqual(settings.api_key, "")
        self.assertEqual(settings.thinking, "disabled")
        self.assertEqual(settings.reasoning_effort, "high")
        self.assertEqual(settings.max_output_tokens, 300_000)
        self.assertEqual(settings.request_timeout_seconds, 120)
        self.assertEqual(settings.request_deadline_seconds, 3600)
        self.assertEqual(settings.limits.max_total_tokens, 1_000_000)
        self.assertEqual(settings.limits.max_seconds, 3600)
        self.assertEqual(settings.limits.max_steps, 100)
        self.assertEqual(settings.max_retries, 2)
        self.assertEqual(settings.temperature, 0)

    def test_environment_overrides_preserve_existing_options(self) -> None:
        values = {
            "DEEPSEEK_API_KEY": "unit-test-secret-not-a-real-key",
            "DEEPSEEK_MODEL": "deepseek-v4-pro",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
            "DEEPSEEK_THINKING": "enabled",
            "DEEPSEEK_REASONING_EFFORT": "max",
            "DEEPSEEK_MAX_TOKENS": "8192",
            "DEEPSEEK_TIMEOUT": "5.5",
            "DEEPSEEK_DEADLINE": "10.5",
            "DEEPSEEK_MAX_RETRIES": "0",
            "MA_TEMPERATURE": "0",
        }
        with patch.dict(os.environ, values, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.api_key, values["DEEPSEEK_API_KEY"])
        self.assertEqual(settings.model, "deepseek-v4-pro")
        self.assertEqual(settings.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(settings.thinking, "enabled")
        self.assertEqual(settings.reasoning_effort, "max")
        self.assertEqual(settings.max_output_tokens, 8192)
        self.assertEqual(settings.request_timeout_seconds, 5.5)
        self.assertEqual(settings.request_deadline_seconds, 10.5)
        self.assertEqual(settings.max_retries, 0)
        self.assertEqual(settings.temperature, 0)

    def test_only_process_environment_is_read(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_MODEL": "deepseek-v4-pro"}, clear=True):
            with patch("builtins.open", side_effect=AssertionError("configuration must not read .env")):
                settings = Settings.from_env()
        self.assertEqual(settings.model, "deepseek-v4-pro")
        self.assertEqual(settings.api_key, "")

    def test_api_key_is_omitted_from_repr(self) -> None:
        settings = Settings(api_key="secret-unique-sentinel")
        self.assertNotIn("secret-unique-sentinel", repr(settings))
        self.assertNotIn("api_key=", repr(settings))
        self.assertEqual(settings.api_key, "secret-unique-sentinel")

    def test_api_key_requires_string_without_echoing_input(self) -> None:
        for value in (None, True, 123, ["secret-unique-sentinel"]):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(ValueError, "api_key must be a string") as raised:
                    Settings(api_key=value)
                self.assertNotIn("secret-unique-sentinel", str(raised.exception))

    def test_supported_models_and_official_base_paths(self) -> None:
        for model in ("deepseek-flash", "deepseek-v4-pro"):
            for base_url in ("https://api.deepseek.com", "https://api.deepseek.com/", "https://api.deepseek.com/v1", "https://api.deepseek.com/v1/"):
                with self.subTest(model=model, base_url=base_url):
                    self.assertIsNone(validate_provider_options(model=model, base_url=base_url))

    def test_model_aliases_and_nonstrings_are_rejected(self) -> None:
        for value in ("deepseek-chat", "deepseek-reasoner", "", "arbitrary-model", None, True, ["deepseek-flash"]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "model must be"):
                    Settings(model=value)

    def test_invalid_base_urls_fail_without_echoing_userinfo(self) -> None:
        for value in (
            "http://api.deepseek.com", "api.deepseek.com", "https:///v1",
            "https://secret-unique-sentinel@api.deepseek.com",
            "https://user:secret-unique-sentinel@api.deepseek.com",
            "https://@api.deepseek.com", "https://api.deepseek.com?key=secret-unique-sentinel",
            "https://api.deepseek.com#secret-unique-sentinel", "https://api.deepseek.com?",
            "https://api.deepseek.com#", " https://api.deepseek.com", "https://api.deepseek.com\n",
            "https://api.deepseek.com/path with space", "https://api.deepseek.com:invalid",
            "https://api.deepseek.com:65536", "https://api.deepseek.com:0", "https://api.deepseek.com:",
            "https://[invalid", None, 1, True,
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "base_url must be") as raised:
                    Settings(base_url=value)
                self.assertNotIn("secret-unique-sentinel", str(raised.exception))

    def test_literal_options_are_strict_even_when_not_sent(self) -> None:
        for field, values in {
            "thinking": ("on", "Enabled", "", True, 1, None, []),
            "reasoning_effort": ("medium", "HIGH", "", True, 1, None, []),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        Settings(**{field: value})
        for effort in ("low", "high", "max"):
            self.assertEqual(Settings(thinking="disabled", reasoning_effort=effort).reasoning_effort, effort)

    def test_integer_options_reject_bool_float_and_strings(self) -> None:
        for field in ("max_output_tokens", "max_retries"):
            for value in (True, False, 1.0, "1", None, [], float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        Settings(**{field: value})

    def test_integer_ranges_include_boundaries(self) -> None:
        for value in (1, 300000, 393216):
            self.assertEqual(Settings(max_output_tokens=value).max_output_tokens, value)
        for value in (0, 10):
            self.assertEqual(Settings(max_retries=value).max_retries, value)
        for field, values in {"max_output_tokens": (0, -1, 393217, 500000), "max_retries": (-1, 11)}.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        Settings(**{field: value})

    def test_numeric_options_require_finite_real_values(self) -> None:
        for field in ("temperature", "request_timeout_seconds", "request_deadline_seconds"):
            for value in (True, False, "1", None, [], float("nan"), float("inf"), float("-inf"), 10 ** 1000):
                with self.subTest(field=field, value_type=type(value).__name__):
                    with self.assertRaisesRegex(ValueError, field):
                        Settings(**{field: value})

    def test_temperature_range_and_positive_timeouts(self) -> None:
        for value in (0, 0.0, 0.5, 2, 2.0):
            self.assertEqual(Settings(temperature=value).temperature, value)
        for value in (-0.001, 2.001):
            with self.assertRaisesRegex(ValueError, "temperature"):
                Settings(temperature=value)
        for field in ("request_timeout_seconds", "request_deadline_seconds"):
            for value in (0, -0.1):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        Settings(**{field: value})
            self.assertEqual(getattr(Settings(**{field: 0.01}), field), 0.01)
        # The provider caps an attempt's timeout at the remaining whole-request deadline.
        settings = Settings(request_timeout_seconds=60, request_deadline_seconds=1)
        self.assertEqual(settings.request_deadline_seconds, 1)

    def test_environment_parse_errors_never_echo_values_or_key(self) -> None:
        secret = "secret-unique-sentinel"
        for name in ("DEEPSEEK_MAX_TOKENS", "DEEPSEEK_TIMEOUT", "DEEPSEEK_DEADLINE", "DEEPSEEK_MAX_RETRIES", "MA_TEMPERATURE"):
            with self.subTest(name=name):
                with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret, name: secret}, clear=True):
                    try:
                        Settings.from_env()
                    except ValueError as exc:
                        self.assertIn(name, str(exc))
                        self.assertNotIn(secret, "".join(traceback.format_exception(exc)))
                    else:
                        self.fail("malformed environment value was accepted")

    def test_environment_values_are_validated_after_parsing(self) -> None:
        for name, value in (
            ("DEEPSEEK_MAX_TOKENS", "0"), ("DEEPSEEK_MAX_TOKENS", "1.0"),
            ("DEEPSEEK_MAX_RETRIES", "11"), ("DEEPSEEK_THINKING", "true"),
            ("DEEPSEEK_REASONING_EFFORT", "medium"), ("DEEPSEEK_MODEL", "deepseek-chat"),
            ("DEEPSEEK_TIMEOUT", "nan"), ("DEEPSEEK_DEADLINE", "inf"),
            ("DEEPSEEK_TIMEOUT", "0"), ("MA_TEMPERATURE", "2.01"),
        ):
            with self.subTest(name=name, value=value):
                with patch.dict(os.environ, {name: value}, clear=True):
                    with self.assertRaises(ValueError):
                        Settings.from_env()

    def test_limits_are_not_shared_between_settings(self) -> None:
        first, second = Settings(), Settings()
        first.limits.max_steps = 99
        self.assertEqual(second.limits.max_steps, 100)

    def test_task_budget_environment_and_recovery_options(self):
        with patch.dict(os.environ, {"MA_MAX_STEPS": "250", "MA_MAX_TOTAL_TOKENS": "5000000",
                                   "MA_MAX_SECONDS": "7200.5", "MA_CONTEXT_WINDOW_TOKENS": "900000",
                                   "MA_OUTPUT_RECOVERY_ATTEMPTS": "1"}, clear=True):
            value = Settings.from_env()
        self.assertEqual((value.limits.max_steps, value.limits.max_total_tokens, value.limits.max_seconds),
                         (250, 5_000_000, 7200.5))
        self.assertEqual(value.context_window_tokens, 900000)
        self.assertEqual(value.max_output_recovery_attempts, 1)

    def test_invalid_budget_configuration_never_silently_disables_a_limit(self):
        for key, values in {"max_steps": [0, 1001, True, "12", 1.5],
                            "max_total_tokens": [0, 100000001, True, "100"],
                            "max_seconds": [0, 86401, float("nan"), float("inf"), True]}.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    configured_limits(Limits(), {key: value})
        with self.assertRaises(ValueError):
            configured_limits(Limits(), {"unknown": 3})
        base = Limits(tool_timeout_seconds=7)
        changed = configured_limits(base, {"max_steps": 300})
        self.assertEqual((base.max_steps, changed.max_steps, changed.tool_timeout_seconds), (100, 300, 7))

    def test_context_and_recovery_ranges_are_strict(self):
        for key, values in {"context_window_tokens": [1023, 1000001, True, "1024"],
                            "max_output_recovery_attempts": [-1, 4, True, 1.0]}.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    Settings(**{key: value})


if __name__ == "__main__":
    unittest.main()
