"""Shared constants for project files."""
from __future__ import annotations

# Directories that are never copied between workspaces or listed to agents.
PROJECT_CACHE_DIRS = frozenset({"__pycache__", "node_modules", "venv", ".venv", ".pytest_cache", ".mypy_cache"})
