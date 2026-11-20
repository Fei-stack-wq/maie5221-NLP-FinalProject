"""Launch real Playwright checks using the same bounded runner as other coding tools."""
from __future__ import annotations

import os
import hmac
import re
import secrets
import sys
import tempfile
from pathlib import Path

from .schemas import ToolResult
from .shell_tool import run_posix_argv


def _browser_cache() -> str:
    # The child has a disposable HOME; give its driver the normal browser binary
    # cache explicitly without exposing the user's cookies or profile.
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return "0" if configured == "0" else str(Path(configured).expanduser().resolve())
    if sys.platform == "darwin":
        return str(Path.home() / "Library" / "Caches" / "ms-playwright")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return str(cache / "ms-playwright")


def run_browser(ctx, args: dict) -> ToolResult:
    if os.name != "posix":
        return ToolResult.failure("run_browser 需要支持进程组回收的 macOS 或 Linux 主机。")
    code = args["code"]
    if not code.strip() or len(code) > 20_000 or "\x00" in code:
        return ToolResult.failure("浏览器脚本须为 1 至 20000 字符的非空 Python 文本。")
    cwd = ctx.sandbox.resolve(args.get("cwd", "."), must_exist=True)
    if not cwd.is_dir():
        return ToolResult.failure("浏览器测试目录必须是项目中的目录。")
    runner = Path(__file__).with_name("playwright_runner.py").resolve()
    with tempfile.TemporaryDirectory(prefix="miniagent-browser-") as temp_home:
        signing_key = secrets.token_bytes(32)
        Path(temp_home, "document-receipt.key").write_bytes(signing_key)
        argv = ["python", "-I", "-B", "-u", str(runner), _browser_cache(), temp_home, code]
        result = run_posix_argv(ctx, argv, cwd, args.get("timeout"))
        receipt = Path(temp_home, "checked-document.sha256")
        if result.ok and receipt.is_file() and not receipt.is_symlink() and receipt.stat().st_size == 96:
            raw = receipt.read_bytes()
            digest, signature = raw[:64], raw[64:]
            if re.fullmatch(b"[a-f0-9]{64}", digest) and hmac.compare_digest(hmac.digest(signing_key, digest, "sha256"), signature):
                result.meta["loopback_document_sha256"] = digest.decode("ascii")
        return result
