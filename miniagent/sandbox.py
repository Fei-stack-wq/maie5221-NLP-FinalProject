"""沙箱：每个 agent 一个私有目录，所有路径都必须落在里面。

对应课程要求 2.2「每个代理必须在自己的私有执行沙箱中运行，并维护自己的专用
文件系统，以防止数据泄露」。

诚实说明这个实现的边界
----------------------
这里是**软隔离**：目录 + 路径校验。它挡住的是"手滑/被诱导去读别的 agent 的文件"
这类越界，以及 ``../../`` 路径穿越；它**挡不住**恶意代码主动访问任意绝对路径
（比如 ``run_python`` 里 ``open(r"C:\\Windows\\...")``）。

要真正的隔离，需要在进程/容器层面做：
  - 每个 agent 独立子进程 + 受限用户
  - Docker 容器，只挂载自己的目录（推荐，但要解决 Windows 上的启动成本）
  - Windows Job Object / Linux namespace

写架构文档时，把"软隔离做到什么程度、硬隔离的成本与取舍"讲清楚，
比假装已经安全更能加分。
"""

from __future__ import annotations

import os
import shlex
import sys
import sysconfig
import tempfile
import threading
import venv
import weakref
from pathlib import Path
from typing import Callable


class SandboxError(RuntimeError):
    """越界访问。会被工具层转换成一条给模型看的错误信息，而不是让进程崩溃。"""


class Sandbox:
    def __init__(self, root: Path | str, agent_name: str = "agent") -> None:
        self.agent_name = agent_name
        self.process_env: dict[str, str] = {}
        self.command_sessions = None
        self.command_tmpdir: Path | None = None
        self.command_wrapper: Callable[[list[str]], list[str]] | None = None
        self.track_shell_group: Callable[[int], None] | None = None
        self._venv: Path | None = None
        self._venv_lock = threading.Lock()
        self._venv_cleanup: weakref.finalize | None = None
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def command_venv(self) -> Path:
        """Share an external, disposable Python environment between this member's commands."""
        with self._venv_lock:
            if self._venv is None:
                storage = tempfile.TemporaryDirectory(prefix="miniagent-python-", dir=self.command_tmpdir)
                path = Path(storage.name).resolve() / "venv"
                try:
                    inherited = dict.fromkeys(sysconfig.get_path(name) for name in ("purelib", "platlib"))
                    share_pip = os.name == "posix" and any((Path(p) / "pip/__main__.py").is_file() for p in inherited)
                    venv.EnvBuilder(with_pip=not share_pip, symlinks=os.name != "nt").create(path)
                    # Keep installed Studio libraries importable while pip installs into
                    # the member prefix. A .pth works with run_python's isolated mode.
                    schemes = sysconfig.get_scheme_names()
                    scheme = "venv" if "venv" in schemes else ("nt" if os.name == "nt" else "posix_prefix")
                    packages = Path(sysconfig.get_path("purelib", scheme, {"base": str(path), "platbase": str(path)}))
                    (packages / "_studio_libraries.pth").write_text("\n".join(inherited) + "\n", encoding="utf-8")
                    if share_pip:
                        # Reuse pip's library without running the host's pip shebang.
                        # This avoids extracting ensurepip for every short-lived member.
                        binaries = path / "bin"
                        command = f'#!/bin/sh\nexec {shlex.quote(str(binaries / "python"))} -B -m pip "$@"\n'
                        for name in ("pip", "pip3", f"pip{sys.version_info.major}.{sys.version_info.minor}"):
                            launcher = binaries / name
                            launcher.write_text(command, encoding="utf-8")
                            launcher.chmod(0o755)
                except BaseException:
                    storage.cleanup()
                    raise
                self._venv_cleanup = weakref.finalize(self, storage.cleanup)
                self._venv = path
            return self._venv

    def close_command_venv(self) -> None:
        with self._venv_lock:
            if self._venv_cleanup is not None:
                self._venv_cleanup()
                self._venv_cleanup = None
                self._venv = None

    # ------------------------------------------------------------------ 路径
    def resolve(self, rel_path: str, *, must_exist: bool = False) -> Path:
        """把"相对路径"解析成沙箱内的绝对路径；任何越界都抛 SandboxError。

        拒绝的输入：
          - 空路径、绝对路径（``/etc/passwd``、``C:\\Windows``）
          - 盘符相对路径（``C:foo``）、UNC（``\\\\server\\share``）
          - 解析后跳出沙箱根的路径（``../``、指向外部的符号链接）
        """
        raw = "" if rel_path is None else str(rel_path).strip()
        if not raw:
            raise SandboxError("路径不能为空")

        normalized = raw.replace("\\", "/")
        if normalized.startswith("/") or normalized.startswith("~"):
            raise SandboxError(f"不允许绝对路径: {raw}")
        if normalized.startswith("//"):
            raise SandboxError(f"不允许网络路径: {raw}")
        if len(normalized) > 1 and normalized[1] == ":":
            raise SandboxError(f"不允许盘符路径: {raw}")

        candidate = Path(normalized)
        if candidate.is_absolute():
            raise SandboxError(f"不允许绝对路径: {raw}")

        try:
            resolved = (self.root / candidate).resolve(strict=False)
        except OSError as exc:  # 极长路径、非法字符等
            raise SandboxError(f"无法解析路径 {raw}: {exc}") from exc

        if not _is_inside(resolved, self.root):
            raise SandboxError(f"路径越界（拒绝访问沙箱之外）: {raw}")

        if must_exist and not resolved.exists():
            raise SandboxError(f"文件不存在: {raw}")
        return resolved

    def relative(self, path: Path) -> str:
        """把绝对路径转回沙箱内的相对路径，用于日志与给模型看。"""
        try:
            return Path(os.path.relpath(path, self.root)).as_posix()
        except ValueError:
            return path.as_posix()

    def ensure_parent(self, path: Path) -> None:
        if not _is_inside(path, self.root):
            raise SandboxError(f"路径越界: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 观测
    def files(self, limit: int = 200) -> list[dict[str, object]]:
        """列出沙箱里的文件（报告里"隔离性"和"产物"都用得上）。"""
        entries: list[dict[str, object]] = []
        for item in sorted(self.root.rglob("*")):
            if len(entries) >= limit:
                break
            if item.is_file():
                entries.append(
                    {"path": self.relative(item), "bytes": item.stat().st_size}
                )
        return entries

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Sandbox(agent={self.agent_name!r}, root={str(self.root)!r})"


def _is_inside(path: Path, root: Path) -> bool:
    """path 是否等于 root 或位于 root 之内（已 resolve，符号链接已展开）。"""
    if path == root:
        return True
    return root in path.parents
