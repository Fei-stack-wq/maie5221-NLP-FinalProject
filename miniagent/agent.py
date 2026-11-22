"""One agent run: a bounded Reason–Act–Observe loop over tool calls.

The loop is deliberately small. Its guarantees are what the old runtime
lacked:

1. **It always terminates.** ``Limits.max_steps`` is finite. When the budget
   (steps, wall clock, tokens) runs out or the loop guard reports a stuck
   pattern, the model gets one final request without tools and must write its
   report. The caller always receives an answer text plus an honest status.
2. **It never forgets silently.** The full conversation is sent every step.
   When it grows past ``compact_at_tokens`` the :class:`ContextManager` prunes
   old tool output and, if needed, replaces the older span with a structured
   checkpoint summary (Pi-style). No sliding window, no "last 4 turns".
3. **Every tool result is answered.** Each assistant tool call receives
   exactly one tool message, including refused, invalid and over-limit calls,
   so the provider history is always valid.
4. **Steering is cooperative.** Messages the user sends while the agent works
   are appended at the next step boundary; the agent is never interrupted in
   the middle of a tool batch.

Loop structure follows OpenAI Agents SDK ``Runner`` (max_turns → final
handling) and Pi ``agent-loop.ts`` (tool batch → steering check → next
turn); see docs/architecture.md for pinned links.
"""

from __future__ import annotations

import os
import shutil
import signal
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

import psutil

from .config import Limits, Settings
from .context import ContextManager
from .loop_guard import LoopGuard
from .provider_http import ProviderError
from .providers import DeepSeekProvider, Provider
from .sandbox import Sandbox
from .schemas import LLMResponse, ToolCall, Usage
from .shell_tool import CommandSessions
from .tools import ToolContext, ToolRegistry

Status = Literal["completed", "max_steps", "stuck", "budget", "cancelled", "error"]

MAX_TOOL_RESULT_CHARS = 24_000
WRAP_UP_INSTRUCTION = (
    "[{reason}] Stop working now — no more tool calls are possible. Write your final report for the "
    "person who assigned this work: what you completed (exact file paths), what you verified and how, "
    "what is still unfinished or uncertain, and what should happen next."
)


class Inbox(Protocol):
    def take(self, agent: str) -> list[str]: ...


@dataclass
class AgentResult:
    agent: str
    status: Status
    answer: str
    error: str = ""
    steps: int = 0
    tool_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    duration_s: float = 0.0
    compactions: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "completed"


class ProcessScope:
    """Background processes started by one agent's shell commands; all killed at the end."""

    def __init__(self) -> None:
        self.groups: set[int] = set()

    def track(self, pgid: int) -> None:
        self.groups.add(pgid)

    def close(self) -> None:
        for pgid in list(self.groups):
            try:
                members = [p for p in psutil.process_iter(["pid"]) if _pgid(p.pid) == pgid]
            except psutil.Error:
                members = []
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            _, alive = psutil.wait_procs(members, timeout=2)
            for process in alive:
                try:
                    process.kill()
                except psutil.Error:
                    pass
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.groups.clear()


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return None


class Agent:
    """Runs one task to completion inside one sandbox."""

    def __init__(self, name: str, provider: Provider, registry: ToolRegistry, sandbox: Sandbox,
                 settings: Settings, *, system_prompt: str, limits: Limits | None = None,
                 on_event: Callable[[dict], None] | None = None,
                 should_cancel: Callable[[], bool] | None = None,
                 inbox: Inbox | None = None, stream: bool = False) -> None:
        self.name = name
        self.provider = provider
        self.registry = registry
        self.sandbox = sandbox
        self.settings = settings
        self.system_prompt = system_prompt
        self.limits = limits or settings.limits
        self.on_event = on_event
        self.should_cancel = should_cancel or (lambda: False)
        self.inbox = inbox
        self.stream = stream

    # ------------------------------------------------------------------
    def emit(self, event_type: str, **fields: Any) -> None:
        """Report progress to the caller. A failing observer never breaks the run."""
        if self.on_event is None:
            return
        try:
            self.on_event({"type": event_type, "agent": self.name, **fields})
        except Exception:  # noqa: BLE001 - UI/trace sinks are best effort
            pass

    def run(self, task: str, *, context: str = "") -> AgentResult:
        started = time.monotonic()
        deadline = started + self.limits.max_seconds if self.limits.max_seconds else None
        first = (context.strip() + "\n\n---\n\n# Your task\n" + task) if context.strip() else task
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": first},
        ]
        ctx_manager = ContextManager(self.settings.compact_at_tokens, self.settings.keep_recent_tokens)
        guard = LoopGuard()
        usage = Usage()
        tool_calls = 0
        recoveries = 0
        empty_answers = 0
        warned = False
        status: Status = "max_steps"
        error = ""
        answer = ""
        step = 0
        processes = ProcessScope()
        tmpdir = tempfile.mkdtemp(prefix="miniagent-run-")
        self.sandbox.command_sessions = CommandSessions()
        self.sandbox.track_shell_group = processes.track
        self.sandbox.command_tmpdir = Path(tmpdir)
        tool_ctx = ToolContext(
            sandbox=self.sandbox, limits=self.limits, approve=lambda _message: True,
            deadline=deadline, should_cancel=self.should_cancel,
            contains_configured_secret=(lambda text: self.settings.api_key in text) if self.settings.api_key else None,
        )
        specs = self.registry.specs(available_only=True)
        tool_schemas = [spec.to_api() for spec in specs]
        self.emit("start", task=task)

        def spend(response: LLMResponse) -> None:
            nonlocal usage
            usage = usage.merged(response.usage)

        def call_model(*, tools_enabled: bool) -> LLMResponse:
            options: dict[str, Any] = {}
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None:
                options["deadline_seconds"] = max(5.0, remaining + 60.0)
            if self.stream and isinstance(self.provider, DeepSeekProvider):
                stream_id = uuid.uuid4().hex
                options["on_stream"] = lambda phase, text: self.emit("delta", stream_id=stream_id, phase=phase, text=text)
                options["should_cancel"] = self.should_cancel
            if specs:
                options["tool_choice"] = "auto" if tools_enabled else "none"
            length_at_request = len(messages)
            response = self.provider.chat(messages, tools=specs or None,
                                          temperature=self.settings.temperature, **options)
            if response.usage.source == "reported" and response.usage.prompt_tokens:
                ctx_manager.note_usage(messages, response.usage.prompt_tokens, length_at_request)
            spend(response)
            return response

        def summarize(prompt: list[dict[str, Any]]):
            response = self.provider.chat(prompt, temperature=0.0)
            spend(response)
            if response.finish_reason == "length":
                raise ValueError("summary was truncated")
            return response.text, response.usage

        def compact(force: bool = False) -> None:
            limit = ctx_manager.compact_at_tokens
            if force:
                ctx_manager.compact_at_tokens = 1
            try:
                result = ctx_manager.maybe_compact(messages, tool_schemas, summarize)
            finally:
                ctx_manager.compact_at_tokens = limit
            if result.applied:
                self.emit("compaction", method=result.method, before_tokens=result.before_tokens,
                          after_tokens=result.after_tokens, error=result.error)

        def take_inbox() -> bool:
            if self.inbox is None:
                return False
            notes = self.inbox.take(self.name)
            for note in notes:
                messages.append({"role": "user", "content": note})
            if notes:
                guard.reset()
                self.emit("steering", count=len(notes))
            return bool(notes)

        try:
            while True:
                if self.should_cancel():
                    status, error = "cancelled", "cancelled by the user"
                    break
                if step >= self.limits.max_steps:
                    status = "max_steps"
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    status, error = "budget", f"time limit of {self.limits.max_seconds:.0f}s reached"
                    break
                if self.limits.max_total_tokens and usage.total >= self.limits.max_total_tokens:
                    status, error = "budget", f"token budget of {self.limits.max_total_tokens} reached"
                    break
                take_inbox()
                left = self.limits.max_steps - step
                if not warned and self.limits.max_steps >= 10 and left <= max(3, self.limits.max_steps // 10):
                    warned = True
                    messages.append({"role": "user", "content": (
                        f"[Budget] Only {left} steps remain for this assignment. Prioritise finishing and "
                        "verifying the most important part, then give your final report.")})
                compact()
                step += 1
                self.emit("step", step=step, max_steps=self.limits.max_steps)
                try:
                    response = call_model(tools_enabled=True)
                except ProviderError as exc:
                    if exc.code == "context_length_exceeded":
                        compact(force=True)
                        response = call_model(tools_enabled=True)
                    else:
                        raise
                messages.append(response.to_assistant_message())
                calls = response.tool_calls
                if not calls:
                    text = response.text.strip()
                    if response.finish_reason == "length" and recoveries < self.settings.max_output_recovery_attempts:
                        recoveries += 1
                        messages.append({"role": "user", "content": (
                            "[Output limit] Your last response was cut off at the output limit. Continue, "
                            "but keep each response shorter: write large files in several smaller tool calls.")})
                        continue
                    if not text:
                        empty_answers += 1
                        if empty_answers <= 2:
                            messages.append({"role": "user", "content": (
                                "You returned neither a tool call nor text. Continue the work with a tool call, "
                                "or write your final report as text.")})
                            continue
                    if take_inbox():
                        continue  # the user said something while the answer was being written
                    answer, status = text, "completed"
                    break
                if response.text.strip():
                    self.emit("say", text=response.text.strip())
                executed = 0
                for call in calls:
                    executed += 1
                    if self.should_cancel():
                        messages.append({"role": "tool", "tool_call_id": call.id,
                                         "content": "Not executed: the task was cancelled."})
                        continue
                    if executed > self.limits.max_tool_calls_per_step:
                        content = (f"Not executed: at most {self.limits.max_tool_calls_per_step} tool calls run "
                                   "per response. Repeat this call in your next response if it is still needed.")
                        messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                        continue
                    content, ok = self._execute(call, tool_ctx)
                    tool_calls += 1
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                    guard.record(call.name, call.arguments, ok, content)
                verdict = guard.check()
                if verdict.nudge:
                    messages.append({"role": "user", "content": verdict.nudge})
                    self.emit("loop_nudge", reason=verdict.reason)
                if verdict.stuck:
                    status, error = "stuck", f"stopped by the loop guard ({verdict.reason})"
                    self.emit("loop_stuck", reason=verdict.reason)
                    break
            if status in {"max_steps", "stuck", "budget"} and not self.should_cancel():
                reason = {"max_steps": f"Step budget of {self.limits.max_steps} reached",
                          "stuck": "Loop detected", "budget": "Budget reached"}[status]
                answer = self._wrap_up(messages, reason, call_model)
        except ProviderError as exc:
            status = "cancelled" if self.should_cancel() else "error"
            error = f"model request failed ({exc.code}): {exc}"
        except Exception as exc:  # noqa: BLE001 - one broken run must not take the team down
            status = "cancelled" if self.should_cancel() else "error"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            sessions = self.sandbox.command_sessions
            self.sandbox.command_sessions = None
            self.sandbox.track_shell_group = None
            if sessions is not None:
                sessions.close()
            processes.close()
            self.sandbox.close_command_venv()
            self.sandbox.command_tmpdir = None
            shutil.rmtree(tmpdir, ignore_errors=True)
        result = AgentResult(agent=self.name, status=status, answer=answer, error=error, steps=step,
                             tool_calls=tool_calls, usage=usage, duration_s=round(time.monotonic() - started, 2),
                             compactions=ctx_manager.compactions)
        self.emit("end", status=status, answer=answer, error=error, steps=step, tool_calls=tool_calls,
                  prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
                  duration_s=result.duration_s)
        return result

    # ------------------------------------------------------------------
    def _execute(self, call: ToolCall, ctx: ToolContext) -> tuple[str, bool]:
        preview = {k: (v if not isinstance(v, str) or len(v) <= 300 else v[:300] + "…") for k, v in call.arguments.items()}
        self.emit("tool_call", tool=call.name, call_id=call.id, arguments=preview)
        if call.parse_error:
            content, ok = (f"Tool call rejected: {call.parse_error}. The arguments were not valid JSON — "
                           "possibly cut off because the call was too large. Split the work into smaller calls."), False
        else:
            result = self.registry.dispatch(call.name, call.arguments, ctx)
            content, ok = result.content, result.ok
        if len(content) > MAX_TOOL_RESULT_CHARS:
            content = content[:MAX_TOOL_RESULT_CHARS] + f"\n…[truncated: {len(content) - MAX_TOOL_RESULT_CHARS} more characters]"
        if not ok and not content.startswith(("Tool call rejected", "Error")):
            content = "Error: " + content
        self.emit("tool_result", tool=call.name, call_id=call.id, ok=ok, preview=content[:600])
        return content, ok

    def _wrap_up(self, messages: list[dict[str, Any]], reason: str, call_model) -> str:
        messages.append({"role": "user", "content": WRAP_UP_INSTRUCTION.format(reason=reason)})
        try:
            response = call_model(tools_enabled=False)
        except ProviderError as exc:
            return f"(No final report: the model request failed — {exc.code}.)"
        messages.append(response.to_assistant_message())
        text = response.text.strip()
        return text or "(The agent stopped without writing a final report.)"
