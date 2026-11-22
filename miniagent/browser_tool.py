"""Launch real Playwright checks using the same bounded runner as other coding tools."""
from __future__ import annotations

import os
import hmac
import json
import secrets
import sys
import tempfile
from pathlib import Path

from .schemas import ToolResult
from .shell_tool import INLINE_FAILURE_LIMIT, run_posix_argv


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
    with tempfile.TemporaryDirectory(prefix="miniagent-browser-", dir=ctx.sandbox.command_tmpdir) as temp_home:
        signing_key = secrets.token_bytes(32)
        Path(temp_home, "document-receipt.key").write_bytes(signing_key)
        argv = ["python", "-I", "-B", "-u", str(runner), _browser_cache(), temp_home, str(ctx.sandbox.root), code]
        result = run_posix_argv(ctx, argv, cwd, args.get("timeout"), failure_evidence_limit=INLINE_FAILURE_LIMIT)
        receipt = Path(temp_home, "checked-document.sha256")
        if (receipt.is_file() and not receipt.is_symlink() and 32 < receipt.stat().st_size <= 160000
                and not result.meta.get("timed_out") and not result.meta.get("cancelled")):
            raw = receipt.read_bytes()
            signature, payload = raw[:32], raw[32:]
            if hmac.compare_digest(hmac.digest(signing_key, payload, "sha256"), signature):
                claims = json.loads(payload)
                status = "passed" if result.ok else "failed"
                named = claims.get("named", {}).get(status, {})
                if named:
                    result.meta["browser_criterion_inputs"] = {
                        key: [{"asserted_browser_files": {path: sha for path, sha in docs if path is not None},
                               "asserted_browser_documents": [sha for path, sha in docs if path is None]}
                              for docs in checks] for key, checks in named.items()}
                documents = claims[status]
                loopback = [sha for path, sha in documents if path is None]
                files = {path:sha for path, sha in documents if path is not None}
                if files:
                    result.meta["asserted_browser_files"] = files
                if loopback:
                    result.meta["asserted_browser_documents"] = loopback
                    if result.ok and len(loopback) == 1:
                        result.meta["loopback_document_sha256"] = loopback[0]
                if not result.ok and not documents and len(claims["passed"]) == 1:
                    # A later Python assertion can fail on a local variable.
                    # Keep its earlier observed page as an investigation hint,
                    # separate from the document that the failing assertion read.
                    result.meta["browser_document_before_failure"] = claims["passed"][0]
        return result
