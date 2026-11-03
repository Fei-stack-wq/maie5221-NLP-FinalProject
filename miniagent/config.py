"""配置与护栏。

为什么单独一个文件：这些数字是"设计决策"，不是零散常量。
在报告里你会需要解释"为什么 max_steps=12" —— 因为单任务平均 6 步、
P95 是 11 步，超出的基本是死循环。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# DeepSeek 当前模型名（2026-10）。flash 便宜快，适合路由/摘要/多数子任务；
# v4-pro 更强更贵，适合规划和难题。详见 docs/agent-dev-primer.md 第 6 节。
DEFAULT_MODEL = "deepseek-flash"


@dataclass
class Limits:
    """护栏。任何一个缺失，agent 都会在某天烧掉你的余额或永远卡住。"""

    max_steps: int = 12
    """最多几轮 LLM 调用。超过就判定 limit_exceeded，而不是无限循环。"""

    max_seconds: float = 120.0
    """单个任务的总墙钟时间上限。"""

    max_total_tokens: int = 200_000
    """单个任务的 token 预算（prompt+completion 累计）。"""

    max_tool_calls_per_step: int = 4
    """单轮模型回复里最多执行几个工具调用，防止一次生成几十个调用。"""

    tool_timeout_seconds: float = 20.0
    """单个工具（尤其是 run_python）的超时。"""


@dataclass
class Settings:
    model: str = DEFAULT_MODEL
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    temperature: float = 0.0
    """默认 0：agent 场景要的是可复现，不是创意。进 agent 的每一步都该能重放。"""

    limits: Limits = field(default_factory=Limits)
    runs_dir: Path = Path("runs")
    """每个 run 一个目录：<runs_dir>/<run_id>/trace.jsonl + agents/<name>/ 产物。"""

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量读取（.env.example 里有全部变量名）。"""
        return cls(
            model=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            temperature=float(os.environ.get("MA_TEMPERATURE", "0")),
        )
