"""Standalone subprocess entry point. Launch the browser before running the member script."""
from __future__ import annotations

import ast
import json
import os
import hashlib
import hmac
import secrets
import shutil
import sys
import tempfile
import weakref
from pathlib import Path
from urllib.parse import unquote, urlsplit


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


class DocumentWitness:
    """Record the actual main-document bytes at the time a DOM assertion executes."""

    def __init__(self, page, api, workspace):
        self.page, self.api, self.document = page, api, None
        self.workspace = workspace
        self.digests = {}
        self.checked = {"passed": set(), "failed": set()}
        self.reads = set()
        self.expectations = weakref.WeakSet()
        self.failed_read = None
        page.on("response", self.navigation)
        for target in (page, page.main_frame):
            original = target.set_content
            def replace(*args, _original=original, **kwargs):
                self.document = None
                return _original(*args, **kwargs)
            target.set_content = replace

    def navigation(self, response):
        if response.request.is_navigation_request() and response.frame == self.page.main_frame:
            self.document = response
            self.digests.clear()

    def current_digest(self):
        response = self.document
        if response is None or self.page.is_closed() or response.status != 200:
            return None
        url = urlsplit(response.url)
        if response.url.partition("#")[0] != self.page.url.partition("#")[0]:
            return None
        path = None
        if url.scheme == "file" and url.netloc in {"", "localhost"}:
            try:
                path = Path(unquote(url.path)).resolve().relative_to(self.workspace).as_posix()
            except (ValueError, OSError):
                return None
        elif url.scheme not in {"http", "https"} or url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return None
        if response not in self.digests:
            try:
                size = response.header_value("content-length")
                if size and (not size.isdecimal() or int(size) > 8 * 1024**2):
                    return None
                body = response.body()
                if len(body) > 8 * 1024**2:
                    return None
                self.digests[response] = (path, hashlib.sha256(body).hexdigest())
            except self.api.Error:
                return None
        return self.digests[response]

    def record(self, digest, status):
        if digest and len(self.checked[status]) < 16:
            self.checked[status].add(digest)

    def check(self, factory, *args, **kwargs):
        result = factory(*args, **kwargs)
        actual = args[0] if args else kwargs.get("actual")
        if factory is self.api.expect and isinstance(actual, self.api.Locator) and actual.page is self.page:
            self.expectations.add(result)
        return result

    def assertion(self, method, *args, **kwargs):
        assertion = getattr(method, "__self__", None)
        if not isinstance(assertion, self.api.LocatorAssertions) or assertion not in self.expectations:
            return method(*args, **kwargs)
        document = self.document
        try:
            value = method(*args, **kwargs)
        except AssertionError:
            if document is self.document:
                self.record(self.current_digest(), "failed")
            raise
        if document is self.document:
            self.record(self.current_digest(), "passed")
        return value

    def read(self, checked, method, *args, **kwargs):
        obj = getattr(method, "__self__", None)
        on_page = obj is self.page or isinstance(obj, self.api.Locator) and obj.page is self.page
        document = self.document
        try:
            value = method(*args, **kwargs)
        except self.api.TimeoutError as exc:
            if on_page and document is self.document:
                self.failed_read = (exc, self.current_digest())
            raise
        if on_page and checked and document is self.document:
            if digest := self.current_digest():
                self.reads.add(digest)
        return value

    def begin(self):
        self.reads.clear()

    def outcome(self, condition):
        condition = bool(condition)
        for digest in self.reads:
            self.record(digest, "passed" if condition else "failed")
        return condition


class AssertionCalls(ast.NodeTransformer):
    """Observe executed native expect calls and DOM reads inside Python assert."""
    getters = {"count","inner_text","text_content","get_attribute","input_value","title","content",
               "is_visible","is_hidden","is_enabled","is_disabled","is_checked","all_text_contents",
               "all_inner_texts","inner_html"}
    in_assert = False

    def __init__(self, name):
        self.name = name

    def invoke(self, method, args, keywords=()):
        return ast.Call(ast.Name(self.name + method, ast.Load()), args, list(keywords))

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "expect":
            return ast.copy_location(self.invoke("check", [node.func, *node.args], node.keywords), node)
        if isinstance(node.func, ast.Attribute) and node.func.attr.startswith(("to_", "not_to_")):
            return ast.copy_location(self.invoke("assertion", [node.func, *node.args], node.keywords), node)
        if isinstance(node.func, ast.Attribute) and node.func.attr in self.getters:
            return ast.copy_location(self.invoke("read", [ast.Constant(self.in_assert), node.func, *node.args], node.keywords), node)
        return node

    def visit_Assert(self, node):
        self.in_assert = True
        node.test = self.visit(node.test)
        self.in_assert = False
        # Later assertions cannot inherit a DOM read from a caught exception.
        reset = ast.Expr(self.invoke("begin", []))
        node.test = self.invoke("outcome", [node.test])
        return [ast.copy_location(reset, node), node]


def main():
    cache, temp_home, workspace, code = sys.argv[1:]
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
        import playwright.sync_api as api
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
                witness = DocumentWitness(page, api, Path(workspace).resolve())
                scope = {"__name__": "__main__", "playwright": playwright, "browser": browser,
                         "context": context, "page": page, "expect": expect}
                prefix = "_studio_" + secrets.token_hex(16) + "_"
                scope.update({prefix + method: getattr(witness, method) for method in ("check","assertion","read","begin","outcome")})
                sys.path.append(os.getcwd())
                tree = AssertionCalls(prefix).visit(ast.parse(code, filename="<run_browser>"))
                script = compile(ast.fix_missing_locations(tree), "<run_browser>", "exec")
                with Path(temp_home, "checked-document.sha256").open("xb") as receipt:
                    try:
                        exec(script, scope)
                    except Exception as exc:
                        # A failed lookup can occur on the line assigning a value,
                        # before the script reaches its next assert. Only retain
                        # the uncaught timeout from this actual page.
                        if witness.failed_read and exc is witness.failed_read[0]:
                            witness.record(witness.failed_read[1], "failed")
                        raise
                    finally:
                        payload = json.dumps({key: sorted(values, key=lambda v: (v[0] or "",v[1])) for key, values in witness.checked.items()},
                                             separators=(",", ":")).encode()
                        receipt.seek(0)
                        receipt.truncate()
                        receipt.write(hmac.digest(signing_key, payload, "sha256") + payload)
            finally:
                if browser.is_connected():
                    context.close()
        finally:
            browser.close()


if __name__ == "__main__":
    main()
