"""Subprocess entry point for ``run_browser``: launch Chromium, run the agent's script.

Usage: ``python playwright_runner.py <browsers_path> <home> <workspace> <code> [--virtual-time] [--evidence <path>]``
or ``python playwright_runner.py --serve <browsers_path> <home>`` to keep one Chromium
open and run one JSON request per stdin line (``{"cwd": ..., "code": ...}``), answering
each with one JSON line (``{"status", "stdout", "stderr"}``). Every request still gets
its own browser context, so cookies and storage never carry over between checks.
Playwright MCP keeps one browser per session the same way.

The script runs with ``page``, ``context``, ``browser``, ``playwright`` and
``expect`` in scope and the workspace as the current directory. Browser
console errors and uncaught page exceptions are printed at the end, because
they are the most common reason a generated web page "does nothing".
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import json
import os
import shutil
import sys
import traceback
import tempfile
from pathlib import Path


# The runner is launched by absolute path with python -I, without the package on sys.path.
if __package__:
    from .browser_evidence import Recorder, ScriptSteps
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from browser_evidence import Recorder, ScriptSteps
    sys.path.pop(0)


# A failing step used to wait Playwright's default 30 s before it failed: in the Mid_Test runs,
# 50 failing browser checks spent about 21 minutes waiting. Local pages answer quickly, so actions
# and assertions fail after 5 s and navigation after 15 s; a script can pass timeout= for slower steps.
ACTION_TIMEOUT_MS = 5_000
NAVIGATION_TIMEOUT_MS = 15_000


def _launch(chromium, browser_error):
    # Only the browser *launch* has fallbacks for missing binaries; a failing
    # check is never retried in another browser.
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
    raise SystemExit("No Chromium/Chrome found. Run `python -m playwright install chromium` in the Studio "
                     "Python environment, or install Google Chrome.")


def main() -> int:
    browsers, home, workspace, code = sys.argv[1:5]
    if browsers:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers
    os.environ["HOME"] = home
    os.chdir(workspace)
    from playwright.sync_api import Error, expect, sync_playwright

    expect.set_options(timeout=ACTION_TIMEOUT_MS)
    with sync_playwright() as playwright:
        browser = _launch(playwright.chromium, Error)
        try:
            options = sys.argv[5:]
            path = options[options.index("--evidence") + 1] if "--evidence" in options else None
            return _run_script(browser, playwright, expect, Error, workspace, code, "--virtual-time" in options, path)
        finally:
            try:
                browser.close()
            except Error:
                pass


@contextmanager
def _script_contexts(browser, virtual_time, error):
    # Configure both public factories before returning control to the script. browser.new_page()
    # owns its implicit context, so keep Playwright's original factory and close semantics.
    new_context, new_page = browser.new_context, browser.new_page

    def configure(context):
        context.set_default_timeout(ACTION_TIMEOUT_MS)
        context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        if virtual_time:
            context.clock.install()

    @wraps(new_context)
    def configured_context(**options):
        context = new_context(**options)
        configure(context)
        return context

    @wraps(new_page)
    def configured_page(**options):
        page = new_page(**options)
        configure(page.context)
        return page

    browser.new_context, browser.new_page = configured_context, configured_page
    try:
        yield
    finally:
        browser.new_context, browser.new_page = new_context, new_page
        # The warm process belongs to one role, but all its contexts belong to this call.
        for context in browser.contexts:
            try:
                context.close()
            except error:
                pass


def _run_script(browser, playwright, expect, error, workspace: str, code: str, virtual_time: bool = False,
                evidence_path=None) -> int:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    console: list[str] = []
    status = 0
    recorder = None
    file = open(evidence_path, "r+b") if evidence_path else None
    try:
        if file:
            recorder = Recorder(ScriptSteps(code), file)
        with _script_contexts(browser, virtual_time, error):
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.on("console", lambda m: console.append(f"console.{m.type}: {m.text}") if m.type in {"error", "warning"} else None)
            page.on("pageerror", lambda e: console.append(f"uncaught page error: {e}"))
            scope = {"page": page, "context": context, "browser": browser, "playwright": playwright, "expect": expect,
                     "__name__": "__main__", "WORKSPACE": Path(workspace)}
            if recorder:
                scope[recorder.steps.name] = recorder
                program = recorder.steps.compile()
            else:
                program = compile(code, "<run_browser>", "exec")
            exec(program, scope)
    except SystemExit as exc:
        status = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if status and recorder:
            recorder.failure(exc, "script_exit")
    except BaseException as exc:  # noqa: BLE001 - report every failure of the agent's script
        if recorder:
            kind = ("operation_timeout" if isinstance(exc, PlaywrightTimeout) else
                    "assertion_failed" if isinstance(exc, AssertionError) else "script_error")
            recorder.failure(exc, kind)
        traceback.print_exc()
        status = 1
    finally:
        if recorder:
            recorder.progress.close()
        if file:
            file.close()
        if console:
            print("\n[browser console]\n" + "\n".join(console[:60]), file=sys.stderr)
    return status


def _captured(handle) -> str:
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(max(0, size - 200_000))
    return handle.read().decode("utf-8", errors="replace")


def serve() -> int:
    browsers, home = sys.argv[2:4]
    if browsers:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers
    os.environ["HOME"] = home
    # Replies use a private copy of stdout. During a request, descriptors 1 and 2 point at
    # files, so output from the script and from processes it starts is captured, and stray
    # writes can never corrupt a reply.
    answer = os.fdopen(os.dup(1), "w", buffering=1)
    quiet, saved_err = os.open(os.devnull, os.O_WRONLY), os.dup(2)
    os.dup2(quiet, 1)
    from playwright.sync_api import Error, expect, sync_playwright

    expect.set_options(timeout=ACTION_TIMEOUT_MS)
    with sync_playwright() as playwright:
        browser = _launch(playwright.chromium, Error)
        answer.write(json.dumps({"ready": True}) + "\n")
        for line in sys.stdin:
            request = json.loads(line)
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                sys.stdout.flush()
                sys.stderr.flush()
                os.dup2(out.fileno(), 1)
                os.dup2(err.fileno(), 2)
                try:
                    os.chdir(request["cwd"])
                    if not browser.is_connected():  # the previous script closed it
                        browser = _launch(playwright.chromium, Error)
                    status = _run_script(browser, playwright, expect, Error, request["workspace"], request["code"],
                                         request.get("virtual_time", False), request.get("evidence"))
                except BaseException:  # noqa: BLE001 - keep serving after any single failure
                    traceback.print_exc()
                    status = 1
                finally:
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os.dup2(quiet, 1)
                    os.dup2(saved_err, 2)
                reply = {"status": status, "stdout": _captured(out), "stderr": _captured(err)}
            answer.write(json.dumps(reply) + "\n")
        try:
            browser.close()
        except Error:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(serve() if sys.argv[1:2] == ["--serve"] else main())
