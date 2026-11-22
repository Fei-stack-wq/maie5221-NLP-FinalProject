"""Subprocess entry point for ``run_browser``: launch Chromium, run the agent's script.

Usage: ``python playwright_runner.py <browsers_path> <home> <workspace> <code>``

The script runs with ``page``, ``context``, ``browser``, ``playwright`` and
``expect`` in scope and the workspace as the current directory. Browser
console errors and uncaught page exceptions are printed at the end, because
they are the most common reason a generated web page "does nothing".
"""
from __future__ import annotations

import os
import shutil
import sys
import traceback
from pathlib import Path


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

    console: list[str] = []
    with sync_playwright() as playwright:
        browser = _launch(playwright.chromium, Error)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        page.on("console", lambda m: console.append(f"console.{m.type}: {m.text}") if m.type in {"error", "warning"} else None)
        page.on("pageerror", lambda e: console.append(f"uncaught page error: {e}"))
        scope = {"page": page, "context": context, "browser": browser, "playwright": playwright, "expect": expect,
                 "__name__": "__main__", "WORKSPACE": Path(workspace)}
        status = 0
        try:
            exec(compile(code, "<run_browser>", "exec"), scope)
        except SystemExit as exc:
            status = exc.code if isinstance(exc.code, int) else 1
        except BaseException:  # noqa: BLE001 - report every failure of the agent's script
            traceback.print_exc()
            status = 1
        finally:
            if console:
                print("\n[browser console]\n" + "\n".join(console[:60]), file=sys.stderr)
            try:
                browser.close()
            except Error:
                pass
    return status


if __name__ == "__main__":
    raise SystemExit(main())
