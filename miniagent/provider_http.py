"""Bounded JSON-over-HTTPS transport, with no model or tool execution logic.

The monotonic deadline covers admission, backoff, response chunks and return.
Socket waits are capped by the remaining time. Python's blocking DNS resolver,
TLS/connect/header handling inside urllib, and JSON decoding cannot be forcibly
interrupted by this synchronous adapter; their elapsed time is checked when
control returns. This is not a process-level hard wall-clock limit.

Public error messages and normally rendered tracebacks omit request/response
bodies, URLs, credentials and raw network exceptions. Exception contexts and
stack locals can still contain sensitive values; do not dump them for logging.
Retry attempts resend only the same serialized HTTP request, never tools or
other application operations.
"""
from __future__ import annotations

import http.client
import json
import math
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any


MAX_RESPONSE_BYTES = 16 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
MAX_RETRY_DELAY = 10.0
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class ProviderError(RuntimeError):
    """Public, sanitized failure metadata. Callers must supply safe messages."""

    def __init__(
        self, message: str = "Provider request failed", *, code: str = "provider_error",
        status_code: int | None = None, attempts: int = 0, retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.attempts = attempts
        self.retryable = retryable


@dataclass(frozen=True)
class HTTPResult:
    data: dict[str, Any]
    attempts: int
    latency_ms: float = 0.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _invalid_request() -> ProviderError:
    return ProviderError("Invalid HTTPS request configuration", code="invalid_request")


def _validate_request(
    url: str, payload: dict, api_key: str, timeout: float,
    deadline_seconds: float, max_retries: int,
) -> None:
    try:
        if type(url) is not str or not url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
            raise ValueError
        url.encode("ascii")
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or "?" in url or "#" in url):
            raise ValueError
        # Accessing .port also rejects malformed and out-of-range ports.
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        if type(payload) is not dict:
            raise ValueError
        if (type(api_key) is not str or not api_key
                or any(not 33 <= ord(char) <= 126 for char in api_key)):
            raise ValueError
        if any(type(value) not in (int, float) or not math.isfinite(value)
               for value in (timeout, deadline_seconds)):
            raise ValueError
        if timeout <= 0 or deadline_seconds < 0 or type(max_retries) is not int or max_retries < 0:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise _invalid_request() from None


def _remaining(deadline: float, attempts: int) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderError("Provider request deadline exceeded", code="deadline_exceeded", attempts=attempts)
    return remaining


def _close(response: Any) -> None:
    try:
        response.close()
    except Exception:
        # Cleanup errors must not expose a raw exception or replace the result.
        pass


def _clamp_socket_timeout(response: Any, seconds: float) -> None:
    # CPython urllib returns http.client.HTTPResponse with this socket chain.
    # Tests/custom urllib handlers may not expose it; open() still gets a bound.
    try:
        sock = response.fp.raw._sock
    except AttributeError:
        return
    sock.settimeout(seconds)


def _read_response(response: Any, deadline: float, timeout: float, attempts: int) -> bytes:
    raw_length = response.headers.get("Content-Length")
    if raw_length is not None:
        try:
            declared_length = int(raw_length)
        except (ValueError, TypeError):
            declared_length = 0
        if declared_length > MAX_RESPONSE_BYTES:
            raise ProviderError("Provider response exceeded the byte limit", code="response_too_large", attempts=attempts)
    reader = getattr(response, "read1", None)
    if not callable(reader):
        raise ProviderError("Provider response does not support bounded reads", code="invalid_response", attempts=attempts)
    chunks: list[bytes] = []
    size = 0
    while True:
        _clamp_socket_timeout(response, min(timeout, _remaining(deadline, attempts)))
        # read1 performs at most one underlying read; read(size) could keep
        # filling from keep-alive whitespace without returning to our deadline.
        chunk = reader(min(READ_CHUNK_BYTES, MAX_RESPONSE_BYTES - size + 1))
        _remaining(deadline, attempts)
        if type(chunk) is not bytes:
            raise ProviderError("Provider response was not bytes", code="invalid_response", attempts=attempts)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ProviderError("Provider response exceeded the byte limit", code="response_too_large", attempts=attempts)
        chunks.append(chunk)


def _parse_response(raw: bytes, attempts: int) -> dict[str, Any]:
    def reject_constant(_value: str) -> None:
        raise ValueError

    def finite_float(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError
        return result

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=reject_constant,
                          parse_float=finite_float, object_pairs_hook=unique_object)
        if type(data) is not dict:
            raise ValueError
        return data
    except (ValueError, UnicodeError, RecursionError):
        raise ProviderError("Provider returned an invalid JSON object", code="invalid_response", attempts=attempts) from None


def _http_error(status: int, attempts: int) -> ProviderError:
    redirect = 300 <= status < 400
    return ProviderError(
        "Provider redirect was refused" if redirect else "Provider returned an HTTP error",
        code="redirect_refused" if redirect else "http_error", status_code=status,
        attempts=attempts, retryable=status in RETRYABLE_STATUS,
    )


def _retry_after(value: str | None, attempts: int, status: int | None) -> float | None:
    if value is None:
        return None
    try:
        if type(value) is not str or not value.strip():
            raise ValueError
        value = value.strip()
        if value.isascii() and value.isdigit():
            delay = float(int(value))
        else:
            instant = parsedate_to_datetime(value)
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            delay = max(0.0, instant.timestamp() - time.time())
        if not math.isfinite(delay):
            raise ValueError
        return delay
    except (ValueError, TypeError, OverflowError):
        raise ProviderError("Provider returned an invalid retry delay", code="invalid_retry_after",
                            status_code=status, attempts=attempts) from None


def post_json(
    url: str, payload: dict, *, api_key: str, timeout: float = 60.0,
    deadline_seconds: float = 120.0, max_retries: int = 2,
) -> HTTPResult:
    """POST one JSON body; max_retries counts *additional* HTTP attempts.

Only HTTPS is accepted; userinfo, query strings, fragments and all redirects
are rejected. The caller chooses the provider host and may restrict it further.
Retry-After is a minimum delay, never shortened to fit our cap or deadline.
"""
    _validate_request(url, payload, api_key, timeout, deadline_seconds, max_retries)
    started = time.monotonic()
    deadline = started + deadline_seconds
    _remaining(deadline, 0)
    try:
        # Serialize once: a retry cannot see later changes to the caller's dict.
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProviderError("Request payload is not JSON serializable", code="invalid_request") from None
    opener = urllib.request.build_opener(_NoRedirect())
    for attempt in range(1, max_retries + 2):
        _remaining(deadline, attempt - 1)
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "Authorization": f"Bearer {api_key}"},
        )
        retry_after = None
        try:
            response = opener.open(request, timeout=min(timeout, _remaining(deadline, attempt - 1)))
            try:
                _remaining(deadline, attempt)
                status = response.getcode()
                if type(status) is not int:
                    raise ProviderError("Provider response lacks an HTTP status", code="invalid_response", attempts=attempt)
                if not 200 <= status < 300:
                    raise urllib.error.HTTPError(url, status, "", response.headers, None)
                raw = _read_response(response, deadline, timeout, attempt)
                data = _parse_response(raw, attempt)
                _remaining(deadline, attempt)
                return HTTPResult(data=data, attempts=attempt,
                                  latency_ms=round((time.monotonic() - started) * 1000, 2))
            finally:
                _close(response)
        except ProviderError:
            raise
        except urllib.error.HTTPError as exc:
            error = _http_error(exc.code, attempt)
            retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
            _close(exc)
        except urllib.error.URLError as exc:
            timed_out = isinstance(exc.reason, TimeoutError)
            error = ProviderError("Provider request timed out" if timed_out else "Provider network request failed",
                                  code="timeout" if timed_out else "network_error", attempts=attempt, retryable=True)
        except TimeoutError:
            error = ProviderError("Provider request timed out", code="timeout", attempts=attempt, retryable=True)
        except (OSError, http.client.HTTPException):
            error = ProviderError("Provider network request failed", code="network_error", attempts=attempt, retryable=True)
        except Exception:
            error = ProviderError("Provider transport failed", code="transport_error", attempts=attempt)
        if not error.retryable or attempt > max_retries:
            raise error from None
        _remaining(deadline, attempt)
        server_delay = _retry_after(retry_after, attempt, error.status_code)
        delay = min(MAX_RETRY_DELAY, 0.5 * (2 ** min(attempt - 1, 5)) + random.uniform(0.0, 0.1))
        if server_delay is not None:
            delay = max(delay, server_delay)
        if delay > MAX_RETRY_DELAY or delay >= _remaining(deadline, attempt):
            raise ProviderError("Retry delay exceeds the configured time allowance", code="retry_after_exceeded",
                                status_code=error.status_code, attempts=attempt, retryable=True) from None
        time.sleep(delay)
    raise AssertionError("unreachable")


__all__ = ["ProviderError", "HTTPResult", "post_json"]
