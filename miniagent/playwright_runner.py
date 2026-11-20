"""Standalone subprocess entry point. Launch the browser before running the member script."""
from __future__ import annotations

import os
import hashlib
import hmac
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def _launch(chromium, browser_error):
    # A failed application check must never be retried against a different
    # browser. Only the browser *launch* has a fallback for missing binaries.
    options = [{}]
    if Path(chromium.executable_path).is_file():
        options.append({"channel": "chromium"})
    options.append({"channel": "chrome"})
    for name in ("chromium", "chromium-browser", "google-chrome"):
        binary = shutil.which(name)
        if binary:
            options.append({"executable_path": binary})
            break
    for selection in options:
        try:
            return chromium.launch(headless=True, **selection)
        except browser_error as exc:
            message = str(exc).lower()
            if not any(s in message for s in ("executable doesn't exist", "distribution 'chrome' is not found",
                                               "executable not found")):
                raise
    raise SystemExit("浏览器环境缺少 Chromium/Chrome。请在 Studio 的 Python 环境执行 "
                     "python -m playwright install chromium，或安装 Google Chrome。")


def main():
    cache, temp_home, code = sys.argv[1:]
    key_file = Path(temp_home, "document-receipt.key")
    signing_key = key_file.read_bytes()
    key_file.unlink()
    # Chromium, driver artifacts and transient downloads live under a fresh
    # temporary directory. Nothing reuses the user's normal Chrome profile.
    os.environ.update(HOME=temp_home, PLAYWRIGHT_BROWSERS_PATH=cache,
                      TMPDIR=temp_home, XDG_CONFIG_HOME=str(Path(temp_home) / "config"),
                      XDG_CACHE_HOME=str(Path(temp_home) / "cache"))
    tempfile.tempdir = temp_home
    try:
        from playwright.sync_api import Error, expect, sync_playwright
    except ModuleNotFoundError as exc:
        if exc.name != "playwright":
            raise
        raise SystemExit("Studio Python 环境缺少 Playwright。请运行 python -m pip install -r requirements.txt，"
                         "然后运行 python -m playwright install chromium。") from None

    with sync_playwright() as playwright:
        browser = _launch(playwright.chromium, Error)
        try:
            context = browser.new_context()
            try:
                page = context.new_page()
                # Retain only the most recent main document. API requests, an
                # asset loaded by another page, and redirects to external hosts
                # cannot attest a loopback application under test.
                documents = []
                def remember(response):
                    if response.request.is_navigation_request() and response.frame == page.main_frame:
                        documents[:] = [response]
                page.on("response", remember)
                scope = {"__name__": "__main__", "playwright": playwright, "browser": browser,
                         "context": context, "page": page, "expect": expect}
                # Load the driver first, then expose project-local test helpers.
                sys.path.append(os.getcwd())
                script = compile(code, "<run_browser>", "exec")
                with Path(temp_home, "checked-document.sha256").open("xb") as receipt:
                    exec(script, scope)
                    if documents and not page.is_closed():
                        response = documents[0]
                        url = urlsplit(response.url)
                        if (response.status == 200 and response.url.split("#")[0] == page.url.split("#")[0]
                                and url.scheme in {"http", "https"} and url.hostname in {"localhost", "127.0.0.1", "::1"}):
                            try:
                                size = response.header_value("content-length")
                                if not size or (size.isdecimal() and int(size) <= 8 * 1024 * 1024):
                                    body = response.body()
                                    if len(body) <= 8 * 1024 * 1024:
                                        digest = hashlib.sha256(body).hexdigest().encode("ascii")
                                        receipt.seek(0)
                                        receipt.truncate()
                                        receipt.write(digest + hmac.digest(signing_key, digest, "sha256"))
                            except Error:
                                pass  # A transient or closed page cannot attest a document.
            finally:
                if browser.is_connected():
                    context.close()
        finally:
            browser.close()


if __name__ == "__main__":
    main()
