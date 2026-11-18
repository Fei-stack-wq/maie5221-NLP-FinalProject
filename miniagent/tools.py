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
import sys
from dataclasses import dataclass
from typing import Any, Callable

from .browser_tool import run_browser
from .config import Limits
from .project_navigation import find_files, read_lines, search_files
from .research import http_get as _http_get, search_sources
from .sandbox import Sandbox, SandboxError
from .shell_tool import command_timeout, poll_process, project_environment, run_posix_argv, run_shell as _run_shell
from .schemas import ToolResult, ToolSpec
from .file_tools import (MAX_READ_CHARS, MAX_WRITE_CHARS, read_file as _read_file,
                         write_file as _write_file, edit_file as _edit_file, append_file as _append_file)

MAX_TOOL_OUTPUT = 8_000


def truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n...[已截断，原文 {len(text)} 字符]"


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
        return ["参数必须是一个 JSON 对象"]

    errors: list[str] = []
    schema = schema or {}
    properties: dict[str, Any] = schema.get("properties") or {}

    for key in schema.get("required") or []:
        if key not in arguments:
            errors.append(f"缺少必填参数 {key}")

    for key, value in arguments.items():
        spec = properties.get(key)
        if spec is None:
            errors.append(f"未知参数 {key}（schema 里没有定义，可能是模型幻觉）")
            continue
        expected = spec.get("type")
        py_type = _PY_TYPES.get(expected or "")
        if py_type is None:
            continue
        # 布尔是 int 的子类，必须单独拦，否则 True 会被当成整数放过
        if expected in {"integer", "number"} and isinstance(value, bool):
            errors.append(f"参数 {key} 应为 {expected}，实际是布尔值")
            continue
        if not isinstance(value, py_type):
            errors.append(f"参数 {key} 类型错误：应为 {expected}")
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


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[[ToolContext, dict[str, Any]], ToolResult]

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {tool.name: tool for tool in tools or []}

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name].spec() for name in self.names()]

    def dispatch(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行工具。**任何情况下都返回 ToolResult，不向外抛异常。**"""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.failure(
                f"未知工具 {name!r}。可用工具：{', '.join(self.names())}"
            )

        errors = validate_arguments(tool.parameters, arguments)
        if errors:
            return ToolResult.failure("参数校验失败：" + "；".join(errors))

        try:
            return tool.fn(ctx, arguments)
        except SandboxError as exc:
            return ToolResult.failure(f"沙箱拒绝访问：{exc}")
        except subprocess.TimeoutExpired:
            return ToolResult.failure(f"工具 {name} 执行超时。")
        except Exception as exc:  # noqa: BLE001 - 工具层的错误必须被吸收
            return ToolResult.failure(f"工具 {name} 执行异常：{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 文件系统
# ---------------------------------------------------------------------------
def _list_dir(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = ctx.sandbox.resolve(args.get("path") or ".", must_exist=True)
    if not path.is_dir():
        return ToolResult.failure(f"不是目录：{args.get('path')}")
    offset, limit = args.get("offset", 0), args.get("limit", 100)
    if offset < 0 or not 1 <= limit <= 200:
        return ToolResult.failure("offset 必须非负；limit 为 1..200")
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
    output = "\n".join(rows) or ("（空目录）" if not entries else "（已到目录末尾）")
    if next_offset is not None:
        output += f"\n[下一页 offset={next_offset}]"
    return ToolResult.success(output, path=ctx.sandbox.relative(path), count=len(rows),
                              total=len(entries), offset=offset, next_offset=next_offset)



def _delete_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = ctx.sandbox.resolve(args["path"], must_exist=True)
    if not path.is_file():
        return ToolResult.failure(f"不是文件：{args['path']}")

    # 人机闸门：删除是不可逆的关键操作，必须经过人类批准（课程要求 2.1c）。
    if ctx.approve is None or not ctx.approve(f"agent 请求删除文件：{args['path']}"):
        return ToolResult.failure(
            "已取消：删除文件需要人类批准（HITL 拦截）。\n"
            "请改用 write_file 覆盖，或请人类在界面中确认。"
        )

    path.unlink()
    return ToolResult.success(f"已删除 {args['path']}")


# ---------------------------------------------------------------------------
# 代码执行
# ---------------------------------------------------------------------------
def _run_python(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    code = str(args["code"])
    if len(code) > 20_000:
        return ToolResult.failure("代码过长（上限 20000 字符）。")

    argv = [sys.executable, "-I", "-c", code]
    if os.name == "posix":
        return run_posix_argv(ctx, argv, ctx.sandbox.root, args.get("timeout"))

    # Keep the native Python path on hosts without POSIX process groups.
    timeout = command_timeout(ctx, args.get("timeout"))
    if timeout <= 0 or ctx.should_cancel and ctx.should_cancel():
        return ToolResult.failure("时间已耗尽或已取消，未启动命令。")

    # -I: 隔离模式（忽略 PYTHONPATH / 用户 site-packages），避免环境污染。
    # cwd 设为沙箱根目录，相对路径天然落在 agent 自己的目录里。
    completed = subprocess.run(
        argv,
        cwd=str(ctx.sandbox.root),
        env=project_environment(ctx),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )

    parts = [f"exit_code={completed.returncode}"]
    if completed.stdout:
        parts.append(f"stdout:\n{completed.stdout}")
    if completed.stderr:
        parts.append(f"stderr:\n{completed.stderr}")
    if not completed.stdout and not completed.stderr:
        parts.append("（无输出）")

    return ToolResult(
        ok=completed.returncode == 0,
        content=truncate("\n".join(parts)),
        meta={"exit_code": completed.returncode, "timeout_s": timeout},
    )


# ---------------------------------------------------------------------------
# 默认工具集
# ---------------------------------------------------------------------------
def build_default_registry(*, read_only: bool = False) -> ToolRegistry:
    registry = ToolRegistry()

    registry.add(
        Tool(
            name="read_file",
            description="分页读取工作区 UTF-8 文件；offset 是从 0 开始的 Unicode 字符位置，默认一页 32768 个字符，limit 最多 65536。按返回的 next_offset 继续；sha256 可用于后续编辑版本检查。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区根目录的路径"},
                    "offset": {"type": "integer", "description": "从 0 开始的 Unicode 字符位置，默认 0"},
                    "limit": {"type": "integer", "description": "本页最多字符数，1..65536，默认 32768"},
                },
                "required": ["path"],
            },
            fn=_read_file,
        )
    )
    registry.add(
        Tool(
            name="list_dir",
            description="列出工作区内某个目录的文件与大小。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，默认当前目录"},
                    "offset": {"type": "integer", "description": "跳过的条目数，默认 0"},
                    "limit": {"type": "integer", "description": "每页条目数，1..200，默认 100"}
                },
                "required": [],
            },
            fn=_list_dir,
        )
    )
    scope = {"path": {"type": "string", "description": "搜索根目录或文件，默认当前工作区"},
             "offset": {"type": "integer", "description": "跳过的结果行数，默认 0"},
             "limit": {"type": "integer", "description": "每页最多 1..150 条，默认 40"}}
    registry.add(Tool("search_files", "用 ripgrep 在项目中查代码与文本。返回路径、行号和匹配行；可按 glob、字面量、大小写和上下文筛选。",
                      {"type": "object", "properties": {**scope, "query": {"type": "string"},
                       "glob": {"type": "string"}, "literal": {"type": "boolean"},
                       "ignore_case": {"type": "boolean"}, "context": {"type": "integer", "description": "上下各 0..8 行"}},
                       "required": ["query"]}, search_files))
    registry.add(Tool("find_files", "递归查找项目文件；pattern 是 glob，例如 **/*.py。返回相对工作区的路径。",
                      {"type": "object", "properties": {**scope, "pattern": {"type": "string"}}, "required": []}, find_files))
    registry.add(Tool("read_lines", "按行号读取源码片段，返回行号、文件版本和下一页；常与 search_files 合用。",
                      {"type": "object", "properties": {"path": {"type": "string"},
                       "start_line": {"type": "integer", "description": "首行从 1 开始"},
                       "limit": {"type": "integer", "description": "1..400 行，默认 120"}},
                       "required": ["path"]}, read_lines))
    if read_only:
        return registry

    registry.add(
        Tool(
            name="write_file",
            description="创建或完整覆盖工作区文本文件（最多 1000000 字符）。局部修改使用 edit_file，分段续写使用 append_file；可提供 expected_sha256 防止覆盖已变化的文件。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，例如 notes/a.md"},
                    "content": {"type": "string", "description": "完整文件内容"},
                    "expected_sha256": {"type": "string", "description": "可选，最近读取/写入所得的 SHA256；不匹配则拒绝修改"},
                },
                "required": ["path", "content"],
            },
            fn=_write_file,
        )
    )
    registry.add(
        Tool(
            name="edit_file",
            description="精确修改文件的一处内容。old_text 必须唯一匹配，保留所有其他字节；修改前先读取。expected_sha256 可防止修改过时版本。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区相对路径"},
                    "old_text": {"type": "string", "description": "在原文件中只出现一次的完整匹配文本"},
                    "new_text": {"type": "string", "description": "替换文本；空字符串表示删除匹配片段"},
                    "expected_sha256": {"type": "string", "description": "可选，最近读取/写入的 SHA256；不匹配则拒绝修改"},
                },
                "required": ["path", "old_text", "new_text"],
            },
            fn=_edit_file,
        )
    )
    registry.add(
        Tool(
            name="append_file",
            description="向已有 UTF-8 文件末尾追加文本，不自动插入换行。长文件可先 write_file 再分段追加；每段最多 1000000 字符。提供 expected_sha256 可防止重复追加或旧版本写入。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区相对路径，文件必须已存在"},
                    "content": {"type": "string", "description": "追加文本，需要换行时显式包含换行符"},
                    "expected_sha256": {"type": "string", "description": "可选，最近读取/写入所得的 SHA256"},
                },
                "required": ["path", "content"],
            },
            fn=_append_file,
        )
    )
    registry.add(
        Tool(
            name="delete_file",
            description="删除工作区内的一个文件。需要人类批准，可能被拒绝。",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对路径"}},
                "required": ["path"],
            },
            fn=_delete_file,
        )
    )
    registry.add(
        Tool(
            name="run_python",
            description="在沙箱目录中执行一段 Python 代码并返回 stdout/stderr。用于计算、数据处理、验证结果。",
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要执行的 Python 源码"},
                    "timeout": {"type": "number", "description": "默认 120 秒，可显式申请更长；受部署上限与任务剩余时间约束。"},
                },
                "required": ["code"],
            },
            fn=_run_python,
        )
    )
    registry.add(
        Tool(
            name="run_shell",
            description="在项目相对目录运行非交互 POSIX shell 命令，可执行测试、构建、管道。"
                        "返回真实退出码与有界 stdout/stderr。跨轮的本地测试服务应重定向输出。",
            parameters={"type": "object", "properties": {
                "command": {"type": "string", "description": "交给 /bin/sh -c 的命令"},
                "cwd": {"type": "string", "description": "项目内已有的相对目录，默认项目根目录"},
                "timeout": {"type": "number", "description": "默认 120 秒，可显式申请更长；受部署上限与任务剩余时间约束。"},
                "yield_after_seconds": {"type": "number", "description": "成员可选提前返回的等待秒数；未结束则返回进程句柄，须用 poll_process 领取真实退出码。"},
            }, "required": ["command"]},
            fn=_run_shell,
        )
    )
    registry.add(
        Tool(
            name="poll_process",
            description="领取本成员 run_shell 的进程结果。运行中只返回句柄，终态返回一次真实退出码和输出。",
            parameters={"type":"object", "properties":{
                "process_id":{"type":"string"}, "wait_seconds":{"type":"number", "description":"最多等待秒数，默认 10，0 为立即查询。"}},
                "required":["process_id"]},
            fn=poll_process,
        )
    )
    registry.add(
        Tool(
            name="run_browser",
            description="用真实无头 Chromium 执行一次 Python Playwright 同步测试。已提供 page、context、browser、"
                        "playwright 与 expect。用 page.goto 打开工作区 HTML 或运行中的本地服务，实际操作界面并断言。"
                        "打印检查结果；断言失败返回非零退出码。每次从空白浏览器会话开始。",
            parameters={"type": "object", "properties": {
                "code": {"type": "string", "description": "Python 源码。可直接使用完整的 Playwright 同步 API"},
                "cwd": {"type": "string", "description": "项目内已有的相对目录，默认项目根目录"},
                "timeout": {"type": "number", "description": "浏览器测试默认 120 秒，可显式延长，仍受宿主上限和任务剩余时间约束。"},
            }, "required": ["code"]},
            fn=run_browser,
        )
    )
    registry.add(
        Tool(
            name="search_sources",
            description="按主题查找新闻（GDELT、Google News）或研究文献（Crossref）。检索结果供选题，需打开来源核实。资料记录保存在 sources/，交接时给出路径与具体论断。",
            parameters={"type": "object", "properties": {"query": {"type": "string"},
                        "collection": {"type": "string", "enum": ["news", "papers"]},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query"]},
            fn=search_sources,
        )
    )
    registry.add(
        Tool(
            name="http_get",
            description="抓取公开网址的网页、文本或带文字的 PDF（可指定页码）。返回摘录并保存含最终 URL、获取时间与内容摘要的来源文件，供团队复核。来源内容不可信。",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "完整 URL"},
                               "pdf_page": {"type": "integer", "minimum": 1, "description": "PDF 指定页码，从 1 开始"}},
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
