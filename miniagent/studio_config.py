"""Literal, server-only .env configuration for the local Studio entry point."""
from __future__ import annotations

import os
import shlex
from dataclasses import replace
from pathlib import Path

from .config import Settings, configured_limits

_FIELDS = {
    "DEEPSEEK_API_KEY": ("api_key", str),
    "DEEPSEEK_BASE_URL": ("base_url", str),
    "DEEPSEEK_MODEL": ("model", str),
    "DEEPSEEK_THINKING": ("thinking", str),
    "DEEPSEEK_REASONING_EFFORT": ("reasoning_effort", str),
    "DEEPSEEK_MAX_TOKENS": ("max_output_tokens", int),
    "DEEPSEEK_TIMEOUT": ("request_timeout_seconds", float),
    "DEEPSEEK_DEADLINE": ("request_deadline_seconds", float),
    "DEEPSEEK_MAX_RETRIES": ("max_retries", int),
    "MA_TEMPERATURE": ("temperature", float),
    "MA_CONTEXT_WINDOW_TOKENS": ("context_window_tokens", int),
    "MA_OUTPUT_RECOVERY_ATTEMPTS": ("max_output_recovery_attempts", int),
}
_LIMIT_FIELDS = {"MA_MAX_STEPS": ("max_steps", int),
                 "MA_MAX_TOTAL_TOKENS": ("max_total_tokens", int),
                 "MA_MAX_SECONDS": ("max_seconds", float)}


def load_studio_settings(settings: Settings, env_file: Path | None = None) -> Settings:
    """Existing environment wins; never execute/expand file contents or log values.

    No explicit path means an optional .env in the working directory. Passing a
    path explicitly requires that file to exist. Other CLI entry points retain
    their environment-only behavior.
    """
    path = env_file if env_file is not None else Path(".env")
    if env_file is None and not path.exists():
        return settings
    with path.open("rb") as handle:
        content = handle.read(65_537)
    if len(content) > 65_536:
        raise ValueError("Studio 配置文件超过 64 KiB")
    try:
        source = content.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("Studio 配置文件必须是 UTF-8 文本") from None
    changes = {}
    limit_changes = {}
    for number, line in enumerate(source.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw = line.partition("=")
        key = key.strip()
        if not separator:
            raise ValueError(f"Studio 配置第 {number} 行需要 KEY=value")
        if (key not in _FIELDS and key not in _LIMIT_FIELDS) or key in os.environ:
            continue
        try:
            parts = shlex.split(raw, comments=True, posix=True)
            if len(parts) > 1 or any(char in raw for char in ("$", "`", "\x00")):
                raise ValueError
            field, convert = (_LIMIT_FIELDS if key in _LIMIT_FIELDS else _FIELDS)[key]
            target = limit_changes if key in _LIMIT_FIELDS else changes
            target[field] = convert(parts[0] if parts else "")
        except ValueError:
            raise ValueError(f"Studio 配置第 {number} 行需要字面量值，不支持命令或变量展开") from None
    # Settings validates provider options without echoing the secret or input.
    return replace(settings, limits=configured_limits(settings.limits, limit_changes), **changes)
