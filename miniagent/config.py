"""配置与护栏。

为什么单独一个文件：这些数字是"设计决策"，不是零散常量。
在报告里你会需要解释"为什么 max_steps=12" —— 因为单任务平均 6 步、
P95 是 11 步，超出的基本是死循环。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

# M0-03 的官方 DeepSeek 模型配置；新增模型需同步协议验证与文档。
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
SUPPORTED_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})


def _integer(name: str, value: object, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")


def _number(name: str, value: object, minimum: float, maximum: float | None = None) -> None:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{name} must be a finite number")
    if maximum is None:
        if value <= minimum:
            raise ValueError(f"{name} must be greater than {minimum}")
    elif not minimum <= value <= maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")


def validate_provider_options(
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    thinking: Literal["enabled", "disabled"] = "disabled",
    reasoning_effort: Literal["low", "high", "max"] = "high",
    max_output_tokens: int = 4096,
    request_timeout_seconds: float = 60.0,
    request_deadline_seconds: float = 120.0,
    max_retries: int = 2,
    temperature: float = 0.0,
) -> None:
    """Validate settings and direct provider calls without I/O or value echoes.

    max_retries counts additional attempts. A request deadline may be shorter
    than a per-attempt timeout; the provider must use the remaining deadline.
    The caller sends reasoning_effort only when thinking is enabled.
    """
    if type(model) is not str or model not in SUPPORTED_MODELS:
        raise ValueError("model must be deepseek-flash or deepseek-v4-pro")
    if type(base_url) is not str or not base_url or any(ord(c) <= 32 or ord(c) == 127 for c in base_url):
        raise ValueError("base_url must be an HTTPS URL without whitespace or control characters")
    try:
        parsed = urlsplit(base_url)
        valid_url = (
            parsed.scheme == "https" and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and "?" not in base_url and "#" not in base_url
            and not parsed.netloc.endswith(":")
        )
        # Accessing .port also validates malformed or out-of-range ports.
        port = parsed.port
        valid_url = valid_url and (port is None or 1 <= port <= 65535)
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("base_url must be an HTTPS URL without userinfo, query, or fragment")
    if type(thinking) is not str or thinking not in {"enabled", "disabled"}:
        raise ValueError("thinking must be enabled or disabled")
    if type(reasoning_effort) is not str or reasoning_effort not in {"low", "high", "max"}:
        raise ValueError("reasoning_effort must be low, high, or max")
    _integer("max_output_tokens", max_output_tokens, 1, 393216)
    _integer("max_retries", max_retries, 0, 10)
    _number("temperature", temperature, 0, 2)
    _number("request_timeout_seconds", request_timeout_seconds, 0)
    _number("request_deadline_seconds", request_deadline_seconds, 0)


def _env_integer(name: str, default: str) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None


def _env_number(name: str, default: str) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        raise ValueError(f"{name} must be a finite number") from None


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
    base_url: str = DEFAULT_BASE_URL
    api_key: str = field(default="", repr=False)
    temperature: float = 0.0
    """默认 0：agent 场景要的是可复现，不是创意。进 agent 的每一步都该能重放。"""

    limits: Limits = field(default_factory=Limits)
    runs_dir: Path = Path("runs")
    """每个 run 一个目录：<runs_dir>/<run_id>/trace.jsonl + agents/<name>/ 产物。"""

    thinking: Literal["enabled", "disabled"] = "disabled"
    reasoning_effort: Literal["low", "high", "max"] = "high"
    max_output_tokens: int = 4096
    request_timeout_seconds: float = 60.0
    request_deadline_seconds: float = 120.0
    max_retries: int = 2
    """初次请求之外的额外重试次数；0 表示不重试。"""

    def __post_init__(self) -> None:
        if type(self.api_key) is not str:
            raise ValueError("api_key must be a string")
        validate_provider_options(
            model=self.model, base_url=self.base_url,
            thinking=self.thinking, reasoning_effort=self.reasoning_effort,
            max_output_tokens=self.max_output_tokens,
            request_timeout_seconds=self.request_timeout_seconds,
            request_deadline_seconds=self.request_deadline_seconds,
            max_retries=self.max_retries, temperature=self.temperature,
        )

    @classmethod
    def from_env(cls) -> "Settings":
        """只读取当前进程环境变量；不会自动加载 .env（见 .env.example）。"""
        return cls(
            model=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
            base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            temperature=_env_number("MA_TEMPERATURE", "0"),
            thinking=os.environ.get("DEEPSEEK_THINKING", "disabled"),
            reasoning_effort=os.environ.get("DEEPSEEK_REASONING_EFFORT", "high"),
            max_output_tokens=_env_integer("DEEPSEEK_MAX_TOKENS", "4096"),
            request_timeout_seconds=_env_number("DEEPSEEK_TIMEOUT", "60"),
            request_deadline_seconds=_env_number("DEEPSEEK_DEADLINE", "120"),
            max_retries=_env_integer("DEEPSEEK_MAX_RETRIES", "2"),
        )
