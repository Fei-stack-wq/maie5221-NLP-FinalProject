"""工具层：agent 与外部世界交互的唯一通道。

对应课程要求 2.3「行动空间」：
  - 文件系统操作   -> read_file / write_file / edit_file / append_file / list_dir / delete_file
  - 编写、测试、执行代码 -> run_python
  - 互联网搜索与资料交接 -> search_sources / http_get

三条设计原则（比工具本身更重要）：
  1. **工具不抛异常**。任何错误都变成文本结果喂回给模型 —— 模型看到"文件不存在"，
     会自己换个路径重试；如果抛异常，整个任务就死了，完成率直接掉。
  2. **所有工具都过沙箱**。路径一律走 ``Sandbox.resolve``，不允许绝对路径与 ``..``。
  3. **输出必须截断**。工具结果是上下文膨胀的头号来源，一次 cat 大文件就能把预算烧光。
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .checkpoint import PauseRequested
from .browser_tool import run_browser
from .config import Limits
from .project_navigation import find_files, read_lines, search_files
from .research import http_get as _http_get, read_source, search_sources
from .sandbox import Sandbox, SandboxError
from .shell_tool import INLINE_FAILURE_LIMIT, command_timeout, poll_process, project_environment, run_posix_argv, run_shell as _run_shell, stop_command_tree
from .schemas import ToolResult, ToolSpec
from .file_tools import (MAX_READ_CHARS, MAX_WRITE_CHARS, read_file as _read_file,
                         write_file as _write_file, edit_file as _edit_file, append_file as _append_file)

MAX_TOOL_OUTPUT = 8_000


def truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n...[truncated, original length {len(text)} characters]"


# ---------------------------------------------------------------------------
# 参数校验：模型会瞎编参数，这里是最后一道闸门
# ---------------------------------------------------------------------------
_PY_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_arguments(schema: dict[str, Any], arguments: Any) -> list[str]:
    """按 JSON Schema（够用的子集）校验模型给的参数，返回错误列表。"""
    if not isinstance(arguments, dict):
        return ["Arguments must be a JSON object"]

    errors: list[str] = []
    schema = schema or {}
    properties: dict[str, Any] = schema.get("properties") or {}

    for key in schema.get("required") or []:
        if key not in arguments:
            errors.append(f"Missing required parameter {key}")

    for key, value in arguments.items():
        spec = properties.get(key)
        if spec is None:
            errors.append(f"Unknown parameter {key} in tool schema")
            continue
        expected = spec.get("type")
        py_type = _PY_TYPES.get(expected or "")
        if py_type is None:
            continue
        # 布尔是 int 的子类，必须单独拦，否则 True 会被当成整数放过
        if expected in {"integer", "number"} and isinstance(value, bool):
            errors.append(f"Parameter {key} must be {expected}, received boolean")
            continue
        if not isinstance(value, py_type):
            errors.append(f"Parameter {key} must be {expected}")
    return errors


# ---------------------------------------------------------------------------
# 工具定义与注册表
# ---------------------------------------------------------------------------
@dataclass
class ToolContext:
    """工具运行所需的环境。人机闸门（HITL）也挂在这里。"""

    sandbox: Sandbox
    limits: Limits
    approve: Callable[[str], bool] | None = None
    """返回 False 即拒绝。默认 None = 一律拒绝危险操作。"""
    deadline: float | None = None
    should_cancel: Callable[[], bool] | None = None
    contains_configured_secret: Callable[[str], bool] | None = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[[ToolContext, dict[str, Any]], ToolResult]
    available: Callable[[], bool] | None = None
    concurrent: Callable[[list[dict[str, Any]]], bool] | None = None
    """Given the arguments of several calls of this tool in one response, True if they may run at once."""
    read_only: bool = False
    """Changes nothing the agent can observe (reads, searches, fetches): safe to run alongside other reads."""

    read_only: bool = False
    """Safe to execute as a conversational lookup before a task exists."""

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {tool.name: tool for tool in tools or []}

    def remove(self, name: str) -> None:
        self._tools.pop(name, None)

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, *, available_only=False) -> list[ToolSpec]:
        tools = (self._tools[name] for name in self.names())
        return [tool.spec() for tool in tools if not available_only or tool.available is None or tool.available()]

    def dispatch(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行工具。**任何情况下都返回 ToolResult，不向外抛异常。**"""
        tool = self._tools.get(name)
        if tool is None or (tool.available is not None and not tool.available()):
            return ToolResult.failure(
                f"Unknown tool {name!r}. Available tools: {', '.join(self.names())}"
            )

        errors = validate_arguments(tool.parameters, arguments)
        if errors:
            return ToolResult.failure("Invalid arguments: " + "；".join(errors), invalid_arguments=True)

        try:
            return tool.fn(ctx, arguments)
        except PauseRequested:
            raise
        except SandboxError as exc:
            return ToolResult.failure(f"Workspace access denied: {exc}")
        except subprocess.TimeoutExpired:
            return ToolResult.failure(f"Tool {name} timed out.")
        except Exception as exc:  # noqa: BLE001 - 工具层的错误必须被吸收
            return ToolResult.failure(f"Tool {name} failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 文件系统
# ---------------------------------------------------------------------------
def _list_dir(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = ctx.sandbox.resolve(args.get("path") or ".", must_exist=True)
    if not path.is_dir():
        return ToolResult.failure(f"Not a directory: {args.get('path')}")
    offset, limit = args.get("offset", 0), args.get("limit", 100)
    if offset < 0 or not 1 <= limit <= 200:
        return ToolResult.failure("offset must be nonnegative and limit must be 1..200")
    entries = sorted(path.iterdir())
    rows = []
    for item in entries[offset:offset + limit]:
        name = ctx.sandbox.relative(item)
        if item.is_symlink():
            rows.append(f"[link] {name}")
        elif item.is_dir():
            rows.append(f"[dir]  {name}/")
        else:
            rows.append(f"[file] {name}  ({item.stat(follow_symlinks=False).st_size} bytes)")
    next_offset = offset + len(rows) if offset + len(rows) < len(entries) else None
    output = "\n".join(rows) or ("(empty directory)" if not entries else "(end of directory)")
    if next_offset is not None:
        output += f"\n[next offset={next_offset}]"
    return ToolResult.success(output, path=ctx.sandbox.relative(path), count=len(rows),
                              total=len(entries), offset=offset, next_offset=next_offset)



def _delete_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = ctx.sandbox.resolve(args["path"], must_exist=True)
    if not path.is_file():
        return ToolResult.failure(f"Not a file: {args['path']}")

    # 人机闸门：删除是不可逆的关键操作，必须经过人类批准（课程要求 2.1c）。
    if ctx.approve is None or not ctx.approve(f"Delete file {args['path']}?"):
        return ToolResult.failure(
            "Deletion requires human approval.\n"
            "Ask the requester for approval."
        )

    path.unlink()
    return ToolResult.success(f"Deleted {args['path']}")


# ---------------------------------------------------------------------------
# 代码执行
# ---------------------------------------------------------------------------
def _run_python(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    code = str(args["code"])
    if len(code) > 20_000:
        return ToolResult.failure("Code exceeds 20000 characters.")

    argv = ["python", "-I", "-B", "-c", code]
    if os.name == "posix":
        return run_posix_argv(ctx, argv, ctx.sandbox.root, args.get("timeout"), failure_evidence_limit=INLINE_FAILURE_LIMIT)

    # Keep the native Python path on hosts without POSIX process groups.
    timeout = command_timeout(ctx, args.get("timeout"))
    if timeout <= 0 or ctx.should_cancel and ctx.should_cancel():
        return ToolResult.failure("Cancelled or deadline reached before command start.")
    env = project_environment(ctx)
    argv[0] = os.path.join(env["VIRTUAL_ENV"], "Scripts", "python.exe")
    timeout = command_timeout(ctx, args.get("timeout"))
    if timeout <= 0 or ctx.should_cancel and ctx.should_cancel():
        return ToolResult.failure("Cancelled or deadline reached before command start.")

    # -I: 隔离模式（忽略 PYTHONPATH / 用户 site-packages），避免环境污染。
    # cwd 设为沙箱根目录，相对路径天然落在 agent 自己的目录里。
    with subprocess.Popen(argv, cwd=str(ctx.sandbox.root), env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          encoding="utf-8", errors="replace") as process:
        until = time.monotonic() + timeout
        while True:
            left = until - time.monotonic()
            cancelled = bool(ctx.should_cancel and ctx.should_cancel())
            if cancelled or left <= 0:
                stop_command_tree(process)
                process.communicate()
                return ToolResult.failure("Cancelled" if cancelled else "Python command timed out.",
                                          cancelled=cancelled, timed_out=not cancelled, exit_code=process.returncode)
            try:
                stdout, stderr = process.communicate(timeout=min(left, .1))
                break
            except subprocess.TimeoutExpired:
                continue

    parts = [f"exit_code={process.returncode}"]
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    if not stdout and not stderr:
        parts.append("(no output)")

    return ToolResult(
        ok=process.returncode == 0,
        content=truncate("\n".join(parts)),
        meta={"exit_code": process.returncode, "timeout_s": timeout},
    )


# ---------------------------------------------------------------------------
# 默认工具集
# ---------------------------------------------------------------------------
# Reads, searches and fetches run together when one response asks for several of them, as Codex
# marks its read and web-search tools as supporting parallel calls. Fetched sources are saved under
# content-hash names, so parallel fetches never write the same file differently.
READ_ONLY_TOOLS = frozenset({"read_file", "read_files", "read_lines", "list_dir", "find_files", "search_files",
                             "read_source", "http_get", "search_sources"})
READ_FILES_TOTAL = 80_000
READ_FILES_EACH = 20_000


def _read_files(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Several files in one call, as aider keeps read-only files in the chat: fewer round trips per member."""
    paths = [str(p) for p in args.get("paths") or []][:20]
    if not paths:
        return ToolResult.failure("Give 1–20 workspace-relative paths.")
    each = min(int(args.get("limit") or READ_FILES_EACH), READ_FILES_EACH)
    parts, used, cut = [], 0, []
    for path in paths:
        if used >= READ_FILES_TOTAL:
            cut.append(path)
            continue
        try:
            result = _read_file(ctx, {"path": path, "limit": max(1, min(each, READ_FILES_TOTAL - used))})
            body = result.content if result.ok else "Error: " + result.content
        except PauseRequested:
            raise
        except Exception as exc:  # noqa: BLE001 - one unreadable file must not hide the others
            body = f"Error: {exc}"
        parts.append(f"=== {path} ===\n{body}")
        used += len(body)
    if cut:
        parts.append("Not read (80000-character limit for one call): " + ", ".join(cut))
    return ToolResult(ok=True, content="\n\n".join(parts))


def build_default_registry(*, read_only: bool = False) -> ToolRegistry:
    registry = _build_default_registry(read_only=read_only)
    for name in READ_ONLY_TOOLS:
        tool = registry.get(name)
        if tool is not None:
            tool.read_only = True
    return registry


def _build_default_registry(*, read_only: bool = False) -> ToolRegistry:
    registry = ToolRegistry()

    registry.add(
        Tool(
            name="read_file",
            description="Read a UTF-8 workspace file by Unicode character offset. Default page 32768, maximum 65536. Continue at next_offset. Use sha256 for subsequent version checks.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path"},
                    "offset": {"type": "integer", "description": "Zero-based Unicode character offset, default 0"},
                    "limit": {"type": "integer", "description": "Maximum page size, 1..65536, default 32768"},
                },
                "required": ["path"],
            },
            fn=_read_file,
        )
    )
    registry.add(
        Tool(
            name="read_files",
            description=("Read several UTF-8 workspace files in one call (up to 20 paths, 20000 characters each, "
                         "80000 in total). Prefer this to several read_file calls."),
            parameters={"type": "object", "properties": {
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Workspace-relative paths"},
                "limit": {"type": "integer", "description": "Characters per file, at most 20000"}},
                "required": ["paths"]},
            fn=_read_files,
        )
    )
    registry.add(
        Tool(
            name="list_dir",
            description="List workspace files and their sizes.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, default current directory"},
                    "offset": {"type": "integer", "description": "Number of entries to skip, default 0"},
                    "limit": {"type": "integer", "description": "Entries per page, 1..200, default 100"}
                },
                "required": [],
            },
            fn=_list_dir,
        )
    )
    scope = {"path": {"type": "string", "description": "File or directory to search, default workspace root"},
             "offset": {"type": "integer", "description": "Result lines to skip, default 0"},
             "limit": {"type": "integer", "description": "Page size, 1..150, default 40"}}
    registry.add(Tool("search_files", "Search project text with ripgrep. Returns paths, line numbers and matches. Supports globs, literal matches, case options and context.",
                      {"type": "object", "properties": {**scope, "query": {"type": "string"},
                       "glob": {"type": "string"}, "literal": {"type": "boolean"},
                       "ignore_case": {"type": "boolean"}, "context": {"type": "integer", "description": "Context lines on either side, 0..8"}},
                       "required": ["query"]}, search_files))
    registry.add(Tool("find_files", "Find workspace files recursively using a glob, e.g. **/*.py. Returns relative paths.",
                      {"type": "object", "properties": {**scope, "pattern": {"type": "string"}}, "required": []}, find_files))
    registry.add(Tool("read_lines", "Read numbered lines with a version hash and next page. Use after search_files.",
                      {"type": "object", "properties": {"path": {"type": "string"},
                       "start_line": {"type": "integer", "description": "First line, starting at 1"},
                       "limit": {"type": "integer", "description": "1..400 lines, default 120"}},
                       "required": ["path"]}, read_lines))
    registry.add(
        Tool(
            name="read_source",
            description="Read a saved source or discovery record by character offset or keyword.",
            parameters={"type": "object", "properties": {
                "path": {"type": "string", "description": "sources/ path returned by http_get or search_sources"},
                "query": {"type": "string", "description": "Optional case-insensitive literal phrase starting from offset"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12000}}, "required": ["path"]},
            fn=read_source,
        )
    )
    if read_only:
        return registry

    registry.add(
        Tool(
            name="write_file",
            description="Create or replace a workspace text file, up to 1000000 characters. Identical content returns no_change. Use edit_file for local edits and append_file for segments. expected_sha256 prevents overwriting a changed version.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path such as notes/a.md"},
                    "content": {"type": "string", "description": "Complete file content"},
                    "expected_sha256": {"type": "string", "description": "Optional SHA-256 from the previous read or write. A mismatch prevents the change."},
                },
                "required": ["path", "content"],
            },
            fn=_write_file,
        )
    )
    registry.add(
        Tool(
            name="edit_file",
            description="Replace exactly one match, preserving other bytes. Identical replacement returns no_change. Read first and optionally provide expected_sha256.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path"},
                    "old_text": {"type": "string", "description": "Literal text matching exactly once"},
                    "new_text": {"type": "string", "description": "Replacement text, or an empty string to delete the match"},
                    "expected_sha256": {"type": "string", "description": "Optional SHA-256 from the previous read or write"},
                },
                "required": ["path", "old_text", "new_text"],
            },
            fn=_edit_file,
        )
    )
    registry.add(
        Tool(
            name="append_file",
            description="Append text to an existing UTF-8 file. Include any desired newline. Maximum 1000000 characters per call. expected_sha256 prevents stale or duplicate appends.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to an existing file"},
                    "content": {"type": "string", "description": "Text to append, including any desired newlines"},
                    "expected_sha256": {"type": "string", "description": "Optional SHA-256 from the previous read or write"},
                },
                "required": ["path", "content"],
            },
            fn=_append_file,
        )
    )
    registry.add(
        Tool(
            name="delete_file",
            description="Delete a workspace file after human approval.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path"}},
                "required": ["path"],
            },
            fn=_delete_file,
        )
    )
    registry.add(
        Tool(
            name="run_python",
            description="Run Python in the workspace and return stdout and stderr. Suitable for calculations, data processing and checks.",
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source"},
                    "timeout": {"type": "number", "description": "Seconds, default 120. A longer timeout is subject to deployment and task limits."},
                },
                "required": ["code"],
            },
            fn=_run_python,
        )
    )
    registry.add(
        Tool(
            name="run_shell",
            description="Run tests, builds and pipelines via noninteractive Bash in a project directory. Returns the exit code and bounded stdout/stderr. "
                        "Pipelines use pipefail, including when truncating output with sed or tail. Handle failures explicitly within the pipeline scope. "
                        "Redirect output for services that continue between steps.",
            parameters={"type": "object", "properties": {
                "command": {"type": "string", "description": "Bash command, pipeline or multiline script"},
                "cwd": {"type": "string", "description": "Existing workspace-relative directory, default project root"},
                "timeout": {"type": "number", "description": "Seconds, default 120. A longer timeout is subject to deployment and task limits."},
                "yield_after_seconds": {"type": "number", "description": "Optional early yield in seconds. Use poll_process on the handle for the final exit code."},
            }, "required": ["command"]},
            fn=_run_shell,
        )
    )
    registry.add(
        Tool(
            name="poll_process",
            description="Collect this member's run_shell process result. A running process returns a handle. The final output and exit code can be collected once.",
            parameters={"type":"object", "properties":{
                "process_id":{"type":"string"}, "wait_seconds":{"type":"number", "description":"Seconds to wait, default 10. Zero polls immediately."}},
                "required":["process_id"]},
            fn=poll_process,
        )
    )
    registry.add(
        Tool(
            name="run_browser",
            description=("Run a Python Playwright (sync API) script in a real headless Chromium. `page`, `context`, "
                         "`browser`, `playwright`, `expect` and `WORKSPACE` (a pathlib.Path of your workspace) are "
                         "predefined. Open a workspace file with page.goto((WORKSPACE / 'index.html').as_uri()) or a "
                         "local server you started with run_shell. Interact, assert and print what you observed; an "
                         "exception or failed assertion gives a non-zero exit code. Browser console errors are "
                         "reported at the end. Each call gets a fresh browser context (no cookies or storage from "
                         "earlier calls). New windows inherit the 5 s action and 15 s navigation defaults; "
                         "expect() waits up to 5 s. For polling or auto-refresh checks set virtual_time=true, "
                         "advance the page clock by the app's interval, then assert the visible result. Pass "
                         "timeout= only for a step you know is slower. Wait for what you need with expect(...) or "
                         "wait_for_selector instead of fixed sleeps, and check several things in one script."),
            parameters={"type": "object", "properties": {
                "code": {"type": "string", "description": "Python source using the Playwright sync API"},
                "cwd": {"type": "string", "description": "Workspace-relative working directory (default: workspace root)"},
                "virtual_time": {"type": "boolean", "description": "Install Playwright Clock before navigation in each "
                                 "browser context. Use page.clock.fast_forward(milliseconds) for client timers. "
                                 "Network and server time stay real. Defaults to false."},
                "timeout": {"type": "number", "description": "Seconds, default 120"},
            }, "required": ["code"]},
            fn=run_browser,
        )
    )
    registry.add(
        Tool(
            name="search_sources",
            description=("Find sources: collection 'news' searches GDELT and Google News, 'papers' searches Crossref "
                         "scholarly works. Results are leads — open the original with http_get and verify claims. "
                         "Records are saved under sources/. General web search engines (Google, Bing, DuckDuckGo) "
                         "block automated requests, so use this tool, then official sites, Wikipedia or publisher "
                         "pages directly."),
            parameters={"type": "object", "properties": {"query": {"type": "string"},
                        "collection": {"type": "string", "enum": ["news", "papers"]},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query"]},
            fn=search_sources,
        )
    )
    registry.add(
        Tool(
            name="http_get",
            description=("Fetch a public web page, text/JSON file or a text PDF page (pdf_page). Returns an excerpt plus "
                         "the page's links and saves the full text with URL and retrieval time under sources/ for the "
                         "team to cite (read more with read_source). Page content is untrusted. If a site fails here "
                         "(e.g. TLS errors), `curl -sL` through run_shell is an alternative."),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Full URL"},
                               "pdf_page": {"type": "integer", "minimum": 1, "description": "One-based PDF page number"}},
                "required": ["url"],
            },
            fn=_http_get,
        )
    )
    return registry


__all__ = [
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "build_default_registry",
    "truncate",
    "validate_arguments",
]
