"""测试用临时目录。

为什么不直接用 ``tempfile``：在部分受限机器上系统临时目录不可写（Windows 沙箱、
公司策略等），测试会全线 PermissionError。放在仓库内的 ``build/tmp`` 永远可写，
且已被 .gitignore 忽略。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

TMP_ROOT = Path(__file__).resolve().parents[1] / "build" / "tmp"


def make_temp_dir(label: str) -> Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / f"{label}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
