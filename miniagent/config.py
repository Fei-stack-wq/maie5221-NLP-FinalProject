"""Runtime configuration: provider options, per-agent budgets and team limits.

Every loop in the system is bounded by default. ``0`` is *not* accepted as
"unlimited" for step counts: an unbounded agent loop with a confused model
spends money forever (see docs/architecture.md, "Why every loop is bounded").
"""

from __future__ import annotations

import math
import os
import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_MAX_OUTPUT_TOKENS = 300_000
DEFAULT_REQUEST_TIMEOUT_SECONDS = 180.0
DEFAULT_REQUEST_DEADLINE_SECONDS = 900.0
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
    thinking: Literal["enabled", "disabled"] = "enabled",
    reasoning_effort: Literal["low", "high", "max"] = "high",
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    request_deadline_seconds: float = DEFAULT_REQUEST_DEADLINE_SECONDS,
    max_retries: int = 2,
    temperature: float = 0.0,
) -> None:
    """Validate provider settings without I/O and without echoing values."""
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


@dataclass
class Limits:
    """Budgets for one agent run (one member assignment or one lead turn)."""

    max_steps: int = 100
    """Model turns for one agent run. Always finite (1..1000)."""

    max_seconds: float = 0
    """Wall clock for one agent run; 0 means no time limit (steps still bound it)."""

    max_total_tokens: int = 0
    """Cumulative prompt+completion tokens for one agent run; 0 only records usage."""

    max_tool_calls_per_step: int = 8
    """Tool calls executed from a single model response; extra calls are refused."""

    tool_timeout_seconds: float = 600.0
    """Upper bound for one foreground command."""

    def __post_init__(self) -> None:
        _integer("max_steps", self.max_steps, 1, 1000)
        _number("max_seconds", self.max_seconds, 0, 86400)
        _integer("max_total_tokens", self.max_total_tokens, 0, 1_000_000_000)
        _integer("max_tool_calls_per_step", self.max_tool_calls_per_step, 1, 32)
        _number("tool_timeout_seconds", self.tool_timeout_seconds, 0)


@dataclass
class TeamLimits:
    """Bounds for one group task (the lead's coordination of the whole team)."""

    lead_max_steps: int = 80
    """Model turns of the lead for one task (delegations are tool calls inside these turns)."""

    max_delegations: int = 24
    """Member assignments the lead may start for one task."""

    max_repeat_assignments: int = 3
    """Near-identical assignments to the same member before the lead is told to change course."""

    mailboxes: bool = False
    interrupts: bool = False
    lead_consult: bool = False

    resume_members: bool = True
    """A member assigned again in the same task continues its earlier conversation instead of starting over."""

    parallel_delegations: bool = True
    """Several delegate_task calls in one lead response run at once when their declared files do not overlap."""

    review_loops: bool = True
    """The lead may run an author–reviewer fix loop (review_loop) without relaying each round itself."""

    plans: bool = True
    """The lead keeps a visible plan with update_plan."""

    plan_links: bool = True
    """Associate hand-offs and their evidence with stable plan step IDs."""

    team_talk: bool = False
    check_ins: int = 3
    teammate_questions: int = 3
    teammate_notes: int = 5
    silence_minutes: float = 10

    retrospective: bool = True
    """After a task, draft lessons for the user to approve into role knowledge."""

    fast: bool = False
    """Fast mode: a time-boxed task where only the lead and reviewers think (see fast_profile)."""

    task_seconds: int = 0
    """Hard wall-clock limit for the whole task; 0 means none. Agents wrap up with a report when it is reached."""

    def __post_init__(self) -> None:
        if type(self.team_talk) is not bool:
            raise ValueError("team_talk must be true or false")
        for name in ("check_ins", "teammate_questions", "teammate_notes"):
            _integer(name, getattr(self, name), 0, 100)
        _number("silence_minutes", self.silence_minutes, 0)
        _integer("lead_max_steps", self.lead_max_steps, 1, 1000)
        _integer("max_delegations", self.max_delegations, 0, 200)
        _integer("max_repeat_assignments", self.max_repeat_assignments, 1, 20)
        _integer("task_seconds", self.task_seconds, 0, 24 * 3600)
        for name in ("mailboxes", "interrupts", "lead_consult", "resume_members", "parallel_delegations", "review_loops", "plans", "plan_links", "retrospective", "fast"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be true or false")


@dataclass
class Settings:
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key: str = field(default="", repr=False)
    temperature: float = 0.0
    limits: Limits = field(default_factory=Limits)
    team: TeamLimits = field(default_factory=TeamLimits)
    runs_dir: Path = Path("runs")
    thinking: Literal["enabled", "disabled"] = "enabled"
    reasoning_effort: Literal["low", "high", "max"] = "high"
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    request_deadline_seconds: float = DEFAULT_REQUEST_DEADLINE_SECONDS
    max_retries: int = 3
    context_window_tokens: int = 1_000_000
    """Provider context window. Compaction starts long before it (see below)."""
    compact_at_tokens: int = 110_000
    """Estimated request size that triggers compaction of older turns."""
    keep_recent_tokens: int = 32_000
    """Recent conversation kept verbatim when compacting (Pi's keepRecentTokens)."""
    max_output_recovery_attempts: int = 2
    interaction_mode: Literal["interactive", "autonomous"] = "interactive"
    warm_browser: bool = True
    """Keep one browser open per agent run for run_browser instead of starting one per call."""
    reviewer_model: str = ""
    """Model for review assignments; empty means the same model as the authors."""
    reviewer_thinking: str = ""
    """Thinking mode for review assignments ("enabled"/"disabled"); empty means the same as the authors."""

    def reviewer(self) -> "Settings":
        """Settings for review assignments (identical to these unless a reviewer model or thinking is set)."""
        return replace(self, model=self.reviewer_model or self.model,
                       thinking=self.reviewer_thinking or self.thinking)

    def member(self) -> "Settings":
        """Settings for members' own work: in fast mode they run without thinking."""
        return replace(self, thinking="disabled") if self.team.fast else self

    @property
    def distinct_reviewer(self) -> bool:
        return (self.reviewer_model or self.model) != self.model or (self.reviewer_thinking or self.thinking) != self.thinking

    def __post_init__(self) -> None:
        if self.interaction_mode not in ("interactive", "autonomous"):
            raise ValueError("interaction_mode must be interactive or autonomous")
        if type(self.api_key) is not str:
            raise ValueError("api_key must be a string")
        _integer("context_window_tokens", self.context_window_tokens, 4096, 1_000_000)
        _integer("compact_at_tokens", self.compact_at_tokens, 2048, self.context_window_tokens)
        _integer("keep_recent_tokens", self.keep_recent_tokens, 512, self.compact_at_tokens)
        _integer("max_output_recovery_attempts", self.max_output_recovery_attempts, 0, 3)
        if self.reviewer_thinking not in ("", "enabled", "disabled"):
            raise ValueError("reviewer_thinking must be empty, enabled or disabled")
        if self.reviewer_model and self.reviewer_model not in SUPPORTED_MODELS:
            raise ValueError("reviewer_model must be empty, deepseek-flash or deepseek-v4-pro")
        validate_provider_options(
            model=self.model, base_url=self.base_url,
            thinking=self.thinking, reasoning_effort=self.reasoning_effort,
            max_output_tokens=self.max_output_tokens,
            request_timeout_seconds=self.request_timeout_seconds,
            request_deadline_seconds=self.request_deadline_seconds,
            max_retries=self.max_retries, temperature=self.temperature,
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        """Read settings from ``env`` (default: the process environment)."""
        env = dict(os.environ if env is None else env)

        def integer(name: str, default: int) -> int:
            try:
                return int(env.get(name, default))
            except ValueError:
                raise ValueError(f"{name} must be an integer") from None

        def flag(name: str, default: bool) -> bool:
            value = env.get(name)
            if value is None or value == "":
                return default
            if value.strip().lower() in ("1", "true", "yes", "on"):
                return True
            if value.strip().lower() in ("0", "false", "no", "off"):
                return False
            raise ValueError(f"{name} must be 0 or 1")

        def number(name: str, default: float) -> float:
            try:
                return float(env.get(name, default))
            except ValueError:
                raise ValueError(f"{name} must be a number") from None

        base = cls()
        return cls(
            model=env.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
            base_url=env.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
            api_key=env.get("DEEPSEEK_API_KEY", ""),
            temperature=number("MA_TEMPERATURE", 0),
            thinking=env.get("DEEPSEEK_THINKING", base.thinking),
            reasoning_effort=env.get("DEEPSEEK_REASONING_EFFORT", "high"),
            max_output_tokens=integer("DEEPSEEK_MAX_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS),
            request_timeout_seconds=number("DEEPSEEK_TIMEOUT", DEFAULT_REQUEST_TIMEOUT_SECONDS),
            request_deadline_seconds=number("DEEPSEEK_DEADLINE", DEFAULT_REQUEST_DEADLINE_SECONDS),
            max_retries=integer("DEEPSEEK_MAX_RETRIES", base.max_retries),
            context_window_tokens=integer("MA_CONTEXT_WINDOW_TOKENS", base.context_window_tokens),
            compact_at_tokens=integer("MA_COMPACT_AT_TOKENS", base.compact_at_tokens),
            keep_recent_tokens=integer("MA_KEEP_RECENT_TOKENS", base.keep_recent_tokens),
            max_output_recovery_attempts=integer("MA_OUTPUT_RECOVERY_ATTEMPTS", 2),
            interaction_mode=env.get("MA_INTERACTION_MODE", "interactive"),
            warm_browser=flag("MA_WARM_BROWSER", True),
            reviewer_model=env.get("MA_REVIEWER_MODEL", ""),
            reviewer_thinking=env.get("MA_REVIEWER_THINKING", ""),
            runs_dir=Path(env.get("MA_RUNS_DIR", "runs")),
            limits=Limits(
                max_steps=integer("MA_MAX_STEPS", Limits.max_steps),
                max_seconds=number("MA_MAX_SECONDS", 0),
                max_total_tokens=integer("MA_MAX_TOTAL_TOKENS", 0),
            ),
            team=TeamLimits(
                lead_max_steps=integer("MA_LEAD_MAX_STEPS", TeamLimits.lead_max_steps),
                max_delegations=integer("MA_MAX_DELEGATIONS", TeamLimits.max_delegations),
                mailboxes=flag("MA_MAILBOXES", TeamLimits.mailboxes),
                interrupts=flag("MA_INTERRUPTS", TeamLimits.interrupts),
                lead_consult=flag("MA_LEAD_CONSULT", TeamLimits.lead_consult),
                resume_members=flag("MA_RESUME_MEMBERS", TeamLimits.resume_members),
                parallel_delegations=flag("MA_PARALLEL_DELEGATIONS", TeamLimits.parallel_delegations),
                review_loops=flag("MA_REVIEW_LOOPS", TeamLimits.review_loops),
                plans=flag("MA_PLANS", TeamLimits.plans),
                plan_links=flag("MA_PLAN_LINKS", TeamLimits.plan_links),
                team_talk=flag("MA_TEAM_TALK", TeamLimits.team_talk),
                retrospective=flag("MA_RETROSPECTIVE", TeamLimits.retrospective),
            ),
        )

    def with_limits(self, **changes) -> "Settings":
        return replace(self, limits=replace(self.limits, **changes))


_ENV_KEYS = frozenset({
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DEEPSEEK_THINKING",
    "DEEPSEEK_REASONING_EFFORT", "DEEPSEEK_MAX_TOKENS", "DEEPSEEK_TIMEOUT", "DEEPSEEK_DEADLINE",
    "DEEPSEEK_MAX_RETRIES", "MA_TEMPERATURE", "MA_CONTEXT_WINDOW_TOKENS", "MA_COMPACT_AT_TOKENS",
    "MA_KEEP_RECENT_TOKENS", "MA_OUTPUT_RECOVERY_ATTEMPTS", "MA_INTERACTION_MODE", "MA_RUNS_DIR",
    "MA_MAX_STEPS", "MA_MAX_SECONDS", "MA_MAX_TOTAL_TOKENS", "MA_LEAD_MAX_STEPS", "MA_MAX_DELEGATIONS",
    "MA_RESUME_MEMBERS", "MA_PARALLEL_DELEGATIONS", "MA_REVIEW_LOOPS", "MA_PLANS", "MA_PLAN_LINKS", "MA_RETROSPECTIVE",
    "MA_MAILBOXES", "MA_INTERRUPTS", "MA_LEAD_CONSULT", "MA_TEAM_TALK", "MA_REVIEWER_MODEL", "MA_REVIEWER_THINKING", "MA_WARM_BROWSER",
})


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=value literals from a .env file. Nothing is executed or expanded.

    Unknown keys are ignored. Legacy step/time keys whose value is ``0`` (the
    old "unlimited" convention) are dropped so the bounded defaults apply.
    """
    with path.open("rb") as handle:
        content = handle.read(65_537)
    if len(content) > 65_536:
        raise ValueError(".env exceeds 64 KiB")
    try:
        source = content.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError(".env must be UTF-8 text") from None
    values: dict[str, str] = {}
    for number, line in enumerate(source.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw = line.partition("=")
        key = key.strip()
        if not separator:
            raise ValueError(f".env line {number} needs KEY=value")
        if key not in _ENV_KEYS:
            continue
        try:
            parts = shlex.split(raw, comments=True, posix=True)
        except ValueError:
            raise ValueError(f".env line {number} has an invalid value") from None
        if len(parts) > 1 or any(char in raw for char in ("$", "`", "\x00")):
            raise ValueError(f".env line {number} must be a literal value")
        value = parts[0] if parts else ""
        if key in {"MA_MAX_STEPS", "MA_LEAD_MAX_STEPS"} and value.strip() == "0":
            continue
        values[key] = value
    return values


def load_settings(env_file: Path | None = None) -> Settings:
    """Process environment wins over an optional .env (default: ./.env)."""
    path = env_file if env_file is not None else Path(".env")
    merged: dict[str, str] = {}
    if path.exists():
        merged.update(read_env_file(path))
    elif env_file is not None:
        raise FileNotFoundError(str(env_file))
    merged.update({key: value for key, value in os.environ.items() if key in _ENV_KEYS})
    return Settings.from_env(merged)


FAST_TASK_SECONDS = 15 * 60
FAST_SOFT_MINUTES = 10


def fast_profile(settings: Settings) -> Settings:
    """Fast mode: aim for a usable result in 10–15 minutes and accept a simpler one.

    As in aider's architect mode, the model that decides (the lead, and reviewers) keeps
    thinking, at low effort, while workers run without it; ``deepseek-flash`` throughout.
    The task has a hard 15-minute limit (agents then write their report), a soft
    10-minute budget that warns the lead, smaller step and hand-off budgets, at most two
    review rounds, short tool timeouts, optional event-linked planning and no post-task lessons.
    """
    return replace(
        settings, model="deepseek-flash", reasoning_effort="low",
        reviewer_model="" if settings.reviewer_model == "deepseek-v4-pro" else settings.reviewer_model,
        limits=replace(settings.limits, max_steps=min(settings.limits.max_steps, 30),
                       tool_timeout_seconds=min(settings.limits.tool_timeout_seconds, 120)),
        team=replace(settings.team, fast=True, task_seconds=FAST_TASK_SECONDS,
                     check_ins=min(settings.team.check_ins, 1), teammate_questions=min(settings.team.teammate_questions, 1),
                     teammate_notes=min(settings.team.teammate_notes, 2), silence_minutes=min(settings.team.silence_minutes, 5),
                     lead_max_steps=min(settings.team.lead_max_steps, 25),
                     max_delegations=min(settings.team.max_delegations, 4),
                     plans=settings.team.plan_links and settings.team.plans, retrospective=False))
