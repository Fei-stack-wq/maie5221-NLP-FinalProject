"""Offline HTTP transport tests: bounded retries, deadlines and safe errors."""
from __future__ import annotations

import io
import json
import traceback
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from miniagent.provider_http import ProviderError, post_json


URL = "https://api.deepseek.com/chat/completions"
KEY = "test-key-do-not-expose"
PRIVATE = "private-reasoning-and-request-content"


class Clock:
    def __init__(self):
        self.current = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.current

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.current += delay


class Response:
    def __init__(self, body=b'{"ok":true}', *, headers=None, status=200, chunks=None, on_read=None):
        self.headers = headers or {}
        self.status = status
        self.chunks = list(chunks) if chunks is not None else [body]
        self.on_read = on_read
        self.closed = False
        self.reads = []
        self.socket = Mock()
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=self.socket))

    def getcode(self):
        return self.status

    def read1(self, size):
        self.reads.append(size)
        if self.on_read:
            self.on_read()
        if not self.chunks:
            return b""
        item = self.chunks.pop(0)
        if isinstance(item, Exception):
            raise item
        if type(item) is bytes and len(item) > size:
            self.chunks.insert(0, item[size:])
            return item[:size]
        return item

    def close(self):
        self.closed = True


def http_error(status, headers=None):
    return urllib.error.HTTPError(URL, status, PRIVATE + KEY, headers or {}, io.BytesIO(PRIVATE.encode()))


class ProviderHTTPTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.opener = Mock()
        self.builder = patch("miniagent.provider_http.urllib.request.build_opener", return_value=self.opener).start()
        patch("miniagent.provider_http.time.monotonic", side_effect=self.clock.monotonic).start()
        patch("miniagent.provider_http.time.sleep", side_effect=self.clock.sleep).start()
        patch("miniagent.provider_http.time.time", return_value=1000.0).start()
        patch("miniagent.provider_http.random.uniform", return_value=0.05).start()
        self.addCleanup(patch.stopall)

    def call(self, **kwargs):
        return post_json(URL, {"messages": [{"role": "user", "content": PRIVATE}]}, api_key=KEY, **kwargs)

    def test_success_preserves_json_and_uses_https_post(self):
        response = Response(body='\n\n{"content":"你好"}'.encode())
        self.opener.open.return_value = response
        result = self.call(max_retries=0)
        self.assertEqual(result.data, {"content": "你好"})
        self.assertEqual(result.attempts, 1)
        self.assertEqual(self.clock.sleeps, [])
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
        self.assertEqual(json.loads(request.data)["messages"][0]["content"], PRIVATE)
        self.assertTrue(response.closed)
        self.assertEqual(response.reads, [65536, 65536])

    def test_v1_endpoint_is_accepted_without_rewriting(self):
        self.opener.open.return_value = Response()
        url = "https://api.deepseek.com/v1/chat/completions"
        post_json(url, {}, api_key=KEY)
        self.assertEqual(self.opener.open.call_args.args[0].full_url, url)

    def test_zero_retries_still_attempts_once_and_does_not_sleep(self):
        self.opener.open.side_effect = urllib.error.URLError(PRIVATE + KEY)
        with self.assertRaises(ProviderError) as caught:
            self.call(max_retries=0)
        self.assertEqual(caught.exception.code, "network_error")
        self.assertEqual(caught.exception.attempts, 1)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_max_retries_means_additional_attempts_and_final_failure_never_sleeps(self):
        self.opener.open.side_effect = urllib.error.URLError(PRIVATE)
        with self.assertRaises(ProviderError) as caught:
            self.call(max_retries=2)
        self.assertEqual(caught.exception.attempts, 3)
        self.assertEqual(self.opener.open.call_count, 3)
        self.assertEqual(self.clock.sleeps, [0.55, 1.05])

    def test_retry_body_is_frozen_once_even_if_caller_payload_changes(self):
        payload = {"input": "original"}
        bodies = []

        def open_request(request, timeout):
            bodies.append(request.data)
            if len(bodies) == 1:
                payload["input"] = "changed by caller"
                raise urllib.error.URLError(PRIVATE)
            return Response()

        self.opener.open.side_effect = open_request
        result = post_json(URL, payload, api_key=KEY, max_retries=1)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(json.loads(bodies[1]), {"input": "original"})

    def test_only_selected_http_statuses_retry(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = [http_error(status), Response()]
                self.assertEqual(self.call(max_retries=1).attempts, 2)
                self.assertEqual(self.opener.open.call_count, 2)

    def test_request_and_authentication_errors_never_retry(self):
        for status in (400, 401, 402, 403, 404, 409, 422):
            with self.subTest(status=status):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = http_error(status)
                self.clock.sleeps.clear()
                with self.assertRaises(ProviderError) as caught:
                    self.call()
                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(caught.exception.code, "http_error")
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(self.opener.open.call_count, 1)
                self.assertEqual(self.clock.sleeps, [])

    def test_timeout_and_wrapped_timeout_are_retryable(self):
        for failure in (TimeoutError(PRIVATE + KEY), urllib.error.URLError(TimeoutError(PRIVATE))):
            with self.subTest(failure=type(failure).__name__):
                self.opener.open.side_effect = failure
                with self.assertRaises(ProviderError) as caught:
                    self.call(max_retries=0)
                self.assertEqual(caught.exception.code, "timeout")
                self.assertTrue(caught.exception.retryable)

    def test_response_read_failure_retries_and_closes_failed_response(self):
        failed = Response(chunks=[b'\n', OSError(PRIVATE + KEY)])
        success = Response()
        self.opener.open.side_effect = [failed, success]
        result = self.call(max_retries=1)
        self.assertEqual(result.attempts, 2)
        self.assertTrue(failed.closed)
        self.assertTrue(success.closed)

    def test_retry_after_seconds_is_a_minimum_not_shortened_backoff(self):
        self.opener.open.side_effect = [http_error(429, {"Retry-After": "3"}), Response()]
        result = self.call(max_retries=1)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(self.clock.sleeps, [3.0])

    def test_retry_after_http_date_uses_wall_clock_only_to_find_delay(self):
        date = format_datetime(datetime.fromtimestamp(1004, timezone.utc), usegmt=True)
        self.opener.open.side_effect = [http_error(503, {"Retry-After": date}), Response()]
        self.call(max_retries=1)
        self.assertEqual(self.clock.sleeps, [4.0])

    def test_retry_after_above_cap_or_deadline_stops_without_early_retry(self):
        for delay, deadline in (("11", 120), ("3", 3), ("3", 2)):
            with self.subTest(delay=delay, deadline=deadline):
                self.opener.open.reset_mock()
                self.clock.sleeps.clear()
                self.opener.open.side_effect = http_error(429, {"Retry-After": delay})
                with self.assertRaises(ProviderError) as caught:
                    self.call(deadline_seconds=deadline)
                self.assertEqual(caught.exception.code, "retry_after_exceeded")
                self.assertEqual(caught.exception.status_code, 429)
                self.assertEqual(self.opener.open.call_count, 1)
                self.assertEqual(self.clock.sleeps, [])

    def test_invalid_retry_after_is_not_ignored_to_retry_early(self):
        self.opener.open.side_effect = http_error(429, {"Retry-After": "not-a-delay-" + PRIVATE})
        with self.assertRaises(ProviderError) as caught:
            self.call()
        self.assertEqual(caught.exception.code, "invalid_retry_after")
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_exhausted_retries_do_not_wait_for_retry_after(self):
        self.opener.open.side_effect = http_error(429, {"Retry-After": "10000"})
        with self.assertRaises(ProviderError) as caught:
            self.call(max_retries=0)
        self.assertEqual(caught.exception.code, "http_error")
        self.assertEqual(self.clock.sleeps, [])

    def test_expired_deadline_rejects_before_http_admission(self):
        with self.assertRaises(ProviderError) as caught:
            self.call(deadline_seconds=0)
        self.assertEqual(caught.exception.code, "deadline_exceeded")
        self.assertEqual(caught.exception.attempts, 0)
        self.opener.open.assert_not_called()

    def test_open_and_each_socket_read_timeout_use_remaining_deadline(self):
        def spend():
            self.clock.current += 0.2

        response = Response(chunks=[b'\n', b'{"ok":true}'], on_read=spend)
        self.opener.open.return_value = response
        self.call(timeout=60, deadline_seconds=1)
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 1.0)
        timeouts = [call.args[0] for call in response.socket.settimeout.call_args_list]
        self.assertEqual(len(timeouts), 3)
        for actual, expected in zip(timeouts, (1.0, 0.8, 0.6)):
            self.assertAlmostEqual(actual, expected)

    def test_keep_alive_chunks_cannot_extend_deadline(self):
        def spend():
            self.clock.current += 0.4

        response = Response(chunks=[b'\n'] * 20, on_read=spend)
        self.opener.open.return_value = response
        with self.assertRaises(ProviderError) as caught:
            self.call(deadline_seconds=1)
        self.assertEqual(caught.exception.code, "deadline_exceeded")
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(len(response.reads), 3)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertTrue(response.closed)

    def test_uninterruptible_open_overrun_is_detected_before_reading(self):
        response = Response()

        def slow_open(*args, **kwargs):
            self.clock.current += 2
            return response

        self.opener.open.side_effect = slow_open
        with self.assertRaises(ProviderError) as caught:
            self.call(deadline_seconds=1)
        self.assertEqual(caught.exception.code, "deadline_exceeded")
        self.assertEqual(response.reads, [])
        self.assertTrue(response.closed)

    def test_oversleep_never_admits_another_attempt(self):
        def oversleep(delay):
            self.clock.sleeps.append(delay)
            self.clock.current += 10

        self.opener.open.side_effect = urllib.error.URLError(PRIVATE)
        with patch("miniagent.provider_http.time.sleep", side_effect=oversleep), self.assertRaises(ProviderError) as caught:
            self.call(deadline_seconds=2)
        self.assertEqual(caught.exception.code, "deadline_exceeded")
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_deadline_must_fit_backoff_before_sleeping(self):
        self.opener.open.side_effect = urllib.error.URLError(PRIVATE)
        with self.assertRaises(ProviderError) as caught:
            self.call(deadline_seconds=0.25)
        self.assertEqual(caught.exception.code, "retry_after_exceeded")
        self.assertEqual(self.clock.sleeps, [])

    def test_redirects_are_disabled_and_never_retried(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = http_error(status, {"Location": "https://other-host.example/collect"})
                with self.assertRaises(ProviderError) as caught:
                    self.call()
                self.assertEqual(caught.exception.code, "redirect_refused")
                self.assertEqual(self.opener.open.call_count, 1)
                redirect = self.builder.call_args.args[0]
                self.assertIsNone(redirect.redirect_request(
                    urllib.request.Request(URL), None, status, "", {}, "https://other-host.example/collect"))

    def test_unsafe_urls_are_rejected_without_echoing_values(self):
        for url in ("http://api.deepseek.com/chat/completions", "https://user:" + KEY + "@api.deepseek.com/path",
                    URL + "?key=" + KEY, URL + "#" + PRIVATE, URL + "?", URL + "#",
                    "https:///missing-host", " https://api.deepseek.com", "https://api.deepseek.com\n",
                    "https://api.deepseek.com:invalid/path"):
            with self.subTest(url=url):
                with self.assertRaises(ProviderError) as caught:
                    post_json(url, {}, api_key=KEY)
                self.assertEqual(caught.exception.code, "invalid_request")
                self.assertNotIn(KEY, str(caught.exception))
                self.assertNotIn(PRIVATE, str(caught.exception))
        self.opener.open.assert_not_called()

    def test_invalid_configuration_and_non_json_payload_never_send(self):
        for change in ({"timeout": 0}, {"timeout": True}, {"timeout": float("inf")},
                       {"deadline_seconds": -1}, {"deadline_seconds": float("nan")},
                       {"max_retries": -1}, {"max_retries": True}, {"max_retries": 1.5}):
            with self.subTest(change=change), self.assertRaises(ProviderError) as caught:
                self.call(**change)
            self.assertEqual(caught.exception.code, "invalid_request")
        for payload in ({"value": float("nan")}, {"value": object()}, ["not an object"]):
            with self.subTest(payload=payload), self.assertRaises(ProviderError):
                post_json(URL, payload, api_key=KEY)
        for key in ("", "secret\r\nHeader: injected", "secret key"):
            with self.subTest(key=key), self.assertRaises(ProviderError):
                post_json(URL, {}, api_key=key)
        self.opener.open.assert_not_called()

    def test_non_json_non_object_non_finite_and_invalid_utf8_are_not_retried(self):
        for body in (PRIVATE.encode(), b'[]', b'null', b'{"value":NaN}', b'\xff', b'   '):
            with self.subTest(body=body):
                self.opener.open.reset_mock()
                self.opener.open.return_value = Response(body)
                with self.assertRaises(ProviderError) as caught:
                    self.call()
                self.assertEqual(caught.exception.code, "invalid_response")
                self.assertEqual(self.opener.open.call_count, 1)

    def test_response_size_limit_checks_header_and_actual_chunks(self):
        for response in (Response(headers={"Content-Length": "33"}), Response(body=b" " * 33)):
            with self.subTest(response=response):
                self.opener.open.return_value = response
                with patch("miniagent.provider_http.MAX_RESPONSE_BYTES", 32), self.assertRaises(ProviderError) as caught:
                    self.call()
                self.assertEqual(caught.exception.code, "response_too_large")
                self.assertTrue(response.closed)
        self.assertEqual(self.clock.sleeps, [])

    def test_duplicate_keys_and_float_overflow_are_rejected_at_every_depth(self):
        bodies = (
            b'{"usage":{"prompt_tokens":1},"usage":{"prompt_tokens":0}}',
            b'{"choices":[{"finish_reason":"length","finish_reason":"stop"}]}',
            b'{"usage":{"value":1e999}}',
            b'{"usage":{"value":-1e999}}',
        )
        for body in bodies:
            with self.subTest(body=body):
                self.opener.open.reset_mock()
                self.opener.open.return_value = Response(body)
                with self.assertRaises(ProviderError) as caught:
                    self.call()
                self.assertEqual(caught.exception.code, "invalid_response")
                self.assertEqual(self.opener.open.call_count, 1)

    def test_response_exactly_at_limit_is_allowed(self):
        body = b'{}' + b' ' * 30
        self.opener.open.return_value = Response(body)
        with patch("miniagent.provider_http.MAX_RESPONSE_BYTES", 32):
            self.assertEqual(self.call().data, {})

    def test_raw_network_http_and_response_content_never_appear_in_tracebacks(self):
        failures = [urllib.error.URLError(PRIVATE + KEY), TimeoutError(PRIVATE + KEY), http_error(401)]
        for failure in failures:
            self.opener.open.side_effect = failure
            try:
                self.call(max_retries=0)
            except ProviderError as exc:
                rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                self.assertNotIn(PRIVATE, rendered)
                self.assertNotIn(KEY, rendered)
                self.assertNotIn(URL, str(exc))
            else:
                self.fail("expected a sanitized error")
        self.opener.open.side_effect = None
        self.opener.open.return_value = Response((PRIVATE + KEY).encode())
        with self.assertRaises(ProviderError) as caught:
            self.call()
        self.assertNotIn(PRIVATE, str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))

    def test_public_error_defaults_allow_existing_provider_callers(self):
        error = ProviderError("Safe caller-supplied explanation")
        self.assertEqual(error.code, "provider_error")
        self.assertIsNone(error.status_code)
        self.assertEqual(error.attempts, 0)
        self.assertFalse(error.retryable)

    def test_unexpected_library_error_is_sanitized_without_retry(self):
        self.opener.open.side_effect = ValueError(PRIVATE + KEY)
        with self.assertRaises(ProviderError) as caught:
            self.call()
        self.assertEqual(caught.exception.code, "transport_error")
        self.assertEqual(caught.exception.attempts, 1)
        self.assertNotIn(PRIVATE, str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(self.opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
