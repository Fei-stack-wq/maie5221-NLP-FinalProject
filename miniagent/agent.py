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
5. **A session can be continued.** Passing an :class:`AgentSession` keeps the
   conversation after the run ends. A later ``run`` with the same session
   appends the next assignment to that history instead of starting over, as
   Codex CLI's ``send_input`` does for a spawned agent. The new assignment is
   anchored so compaction never summarizes it away.

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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

import psutil

from .checkpoint import Continuation, PauseRequested
from .browser_tool import BrowserSession
from .config import Limits, Settings
from .context import ContextManager
from .loop_guard import LoopGuard
from .provider_http import ProviderError
from .providers import Provider
from .sandbox import Sandbox
from .schemas import LLMResponse, ToolCall, ToolResult, Usage
from .shell_tool import CommandSessions
from .tools import ToolContext, ToolRegistry

Status = Literal["completed", "dispatched", "max_steps", "stuck", "budget", "cancelled", "error"]

MAX_TOOL_RESULT_CHARS = 24_000
WRAP_UP_INSTRUCTION = (
    "[{reason}] Stop working now — no more tool calls are possible. Write your final report for the "
    "person who assigned this work: what you completed (exact file paths), what you verified and how, "
    "what is still unfinished or uncertain, and what should happen next."
)


FAILED_REQUEST_NOTE = "(No reply: the model request failed.)"
"""Stands in for the missing answer when a session continues after a failed model request."""


class _MessageInterrupt(Exception):
    """An in-progress sample was discarded. Its turn may be taken again with new mail."""


class BatchPaused(Exception):
    """A pause reached part of a concurrent batch; ``done`` holds the calls that finished."""

    def __init__(self, done: dict[int, ToolResult]) -> None:
        super().__init__("batch paused")
        self.done = done


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
    resumed: bool = False
    """True when this run continued an earlier conversation of the same session."""

    @property
    def ok(self) -> bool:
        return self.status in ("completed", "dispatched")


@dataclass
class AgentSession:
    """One agent's conversation, kept between runs so a follow-up assignment continues it.

    A session can be continued only when its last run ended cleanly with a
    final assistant message (no unanswered tool calls). After an error or a
    cancellation the next run starts a fresh conversation.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    context: ContextManager | None = None
    runs: int = 0

    @property
    def resumable(self) -> bool:
        """True when every tool call in the history has its result, so a new message can follow.

        A run that ended with a model error stops between steps, so its history is complete and the
        session can continue instead of starting over (as Codex rebuilds an agent from its recorded
        history). A history cut inside a tool batch is not continued.
        """
        if not self.messages or self.context is None:
            return False
        pending: set = set()
        for message in self.messages:
            role = message.get("role")
            if role == "tool":
                pending.discard(message.get("tool_call_id"))
                continue
            if pending:
                return False
            if role == "assistant":
                pending = {call.get("id") for call in message.get("tool_calls") or []}
        return not pending


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
                 inbox: Inbox | None = None, stream: bool = False, continuation: Continuation | None = None) -> None:
        self.name = name
        self.provider = provider
        self.registry = registry
        self.sandbox = sandbox
        self.settings = settings
        self.system_prompt = system_prompt
        self.report_requested: Callable[[], str | None] = lambda: None
        self.limits = limits or settings.limits
        self.on_event = on_event
        self.should_cancel = should_cancel or (lambda: False)
        self.inbox = inbox
        self.interrupt = inbox.signal(name) if getattr(inbox, "interrupts", False) else None
        self.stream = stream
        self.continuation = continuation

    # ------------------------------------------------------------------
    def emit(self, event_type: str, **fields: Any) -> None:
        """Report progress to the caller. A failing observer never breaks the run."""
        if self.on_event is None:
            return
        try:
            self.on_event({"type": event_type, "agent": self.name, **fields})
        except Exception:  # noqa: BLE001 - UI/trace sinks are best effort
            pass

    def run(self, task: str, *, context: str = "", session: AgentSession | None = None,
            initial_calls=()) -> AgentResult:
        """Run one task.

        A saved pause frame for this agent continues exactly where it paused. Otherwise a
        resumable ``session`` continues that conversation with ``task`` as the next assignment,
        and without either the run starts a new conversation.
        """
        started = time.monotonic()
        deadline = started + self.limits.max_seconds if self.limits.max_seconds else None
        guard = LoopGuard()
        usage = Usage()
        tool_calls = 0
        recoveries = 0
        empty_answers = 0
        warned = False
        single_reads, nudges = 0, 0
        status: Status = "max_steps"
        error = ""
        answer = ""
        step = 0
        pending = []
        next_call = 0
        in_tool = False
        batch_ok, handed_off = True, False
        elapsed = 0.0
        control = self.continuation
        saved = control.frames.pop(self.name, None) if control else None
        if saved:
            messages = saved["messages"]
            step, tool_calls = saved["step"], saved["tool_calls"]
            elapsed = saved["elapsed"]
            if deadline is not None:
                deadline -= elapsed
            usage = Usage.model_validate(saved["usage"])
            ctx_manager = ContextManager(**saved["context"])
            guard.restore(saved["guard"])
            warned, recoveries, empty_answers = saved["warned"], saved["recoveries"], saved["empty_answers"]
            pending = [ToolCall.model_validate(row) for row in saved["pending"]]
            next_call = saved["next_call"]
            in_tool = saved["in_tool"]
            batch_ok, handed_off = saved["batch_ok"], saved["handed_off"]
            resumed = bool(saved.get("resumed"))
            compactions_before = saved.get("compactions_before", ctx_manager.compactions)
        else:
            resumed = session is not None and session.resumable
            if resumed:
                messages = session.messages
                ctx_manager = session.context
                if messages[-1].get("role") == "user":
                    # The last run failed before the model answered; keep the turns alternating.
                    messages.append({"role": "assistant", "content": FAILED_REQUEST_NOTE})
                note = (context.strip() + "\n\n---\n\n# Your next assignment\n" + task) if context.strip() else task
                current = {"role": "user", "content": note}
                messages.append(current)
                ctx_manager.anchor(current)
            else:
                first = (context.strip() + "\n\n---\n\n# Your task\n" + task) if context.strip() else task
                messages = [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": first},
                ]
                ctx_manager = ContextManager(self.settings.compact_at_tokens, self.settings.keep_recent_tokens)
            compactions_before = ctx_manager.compactions
            if initial_calls:
                # These calls belong to the host handoff. The worker starts a fresh model conversation
                # with their receipts, and its normal tool loop owns validation, tracing and checkpoints.
                pending = [ToolCall.model_validate(call) for call in initial_calls]
                header = LLMResponse(tool_calls=pending, reasoning_content="")
                messages.append(header.to_assistant_message())
                step = 1
        if session is not None:
            session.messages, session.context = messages, ctx_manager
            if not saved:
                session.runs += 1
        processes = ProcessScope()
        tmpdir = tempfile.mkdtemp(prefix="miniagent-run-")
        self.sandbox.command_sessions = CommandSessions()
        self.sandbox.browser_session = BrowserSession() if self.settings.warm_browser else None
        if saved:
            self.sandbox.command_sessions.restore(saved["processes"])
        self.sandbox.track_shell_group = processes.track
        self.sandbox.command_tmpdir = Path(tmpdir)
        tool_ctx = ToolContext(
            sandbox=self.sandbox, limits=self.limits, approve=lambda _message: True,
            deadline=deadline, should_cancel=lambda: self.should_cancel() or self._interrupted(),
            contains_configured_secret=(lambda text: self.settings.api_key in text) if self.settings.api_key else None,
        )
        specs = self.registry.specs(available_only=True)
        tool_schemas = [spec.to_api() for spec in specs]
        self.emit("resume" if saved else "start", task=task, resumed=resumed)
        if initial_calls and not saved:
            self.emit("step", step=step, max_steps=self.limits.max_steps, origin="room_turn")

        def spend(response: LLMResponse) -> None:
            nonlocal usage
            usage = usage.merged(response.usage)

        def call_model(*, tools_enabled: bool) -> LLMResponse:
            nonlocal step
            options: dict[str, Any] = {}
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None:
                options["deadline_seconds"] = max(5.0, remaining + 60.0)
            if self.stream and getattr(self.provider, "supports_streaming", False):
                stream_id = uuid.uuid4().hex
                self.emit("delta", stream_id=stream_id, phase="discard", text="")
                options["on_stream"] = lambda phase, text: self.emit("delta", stream_id=stream_id, phase=phase, text=text)
                options["should_cancel"] = lambda: self.should_cancel() or bool(tools_enabled and
                    (self._interrupted() or control and control.requested.is_set()))
            if specs:
                options["tool_choice"] = "auto" if tools_enabled else "none"
            def interrupted_sample():
                nonlocal step
                self.inbox.mark_interrupted(self.name)
                self.emit("delta", phase="discard", stream_id=stream_id if "on_stream" in options else "", text="")
                self.emit("step_interrupted", step=step)
                step -= 1
                raise _MessageInterrupt()

            length_at_request = len(messages)
            asked = time.monotonic()
            try:
                response = self.provider.chat(messages, tools=specs or None,
                                              temperature=self.settings.temperature, **options)
            except ProviderError as exc:
                self.emit("model_call", seconds=round(time.monotonic() - asked, 3), model=self.settings.model,
                          error=exc.code)
                if tools_enabled and control and control.requested.is_set() and not self.should_cancel():
                    step -= 1
                    raise PauseRequested()
                if tools_enabled and self._interrupted() and not self.should_cancel():
                    interrupted_sample()
                raise
            self._metered(response, time.monotonic() - asked, "step" if tools_enabled else "final")
            if response.usage.source == "reported" and response.usage.prompt_tokens:
                ctx_manager.note_usage(messages, response.usage.prompt_tokens, length_at_request)
            spend(response)
            if tools_enabled and self._interrupted() and not self.should_cancel():
                interrupted_sample()
            return response

        def summarize(prompt: list[dict[str, Any]]):
            asked = time.monotonic()
            response = self.provider.chat(prompt, temperature=0.0)
            self._metered(response, time.monotonic() - asked, "compaction")
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
                if control:
                    control.boundary()
                if not pending:
                    take_inbox()
                    if reason := self.report_requested():
                        answer = self._wrap_up(messages, "The lead requested a report: " + reason, call_model, required=True)
                        status = "completed"
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
                        try:
                            response = call_model(tools_enabled=True)
                        except ProviderError as exc:
                            if exc.code != "context_length_exceeded":
                                raise
                            compact(force=True)
                            response = call_model(tools_enabled=True)
                    except _MessageInterrupt:
                        continue
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
                    calls = self._ordered(calls, messages[-1])
                    pending = calls
                    last_batch = [call.name for call in calls]
                    next_call = 0
                    batch_ok, handed_off = True, False
                index = next_call
                while index < len(pending):
                    next_call = index
                    call = pending[index]
                    if control:
                        control.boundary()
                    segment = self._segment(pending, index)
                    if segment and not self.report_requested() and not self.should_cancel():
                        rejoin_batch, in_tool = in_tool, True
                        try:
                            together = self._run_together([pending[i] for i in segment], tool_ctx,
                                                          rejoining=rejoin_batch)
                        except BatchPaused as paused:
                            # Keep what finished; only the paused calls (and any after them) run again on resume.
                            done_at = {segment[k]: result for k, result in paused.done.items()}
                            for at in sorted(done_at):
                                call, result = pending[at], done_at[at]
                                batch_ok = batch_ok and result.ok
                                handed_off = handed_off or result.handed_off
                                tool_calls += 1
                                messages.append({"role": "tool", "tool_call_id": call.id, "content": result.content})
                                guard.record(call.name, call.arguments, result.ok, result.content)
                            pending = [c for at, c in enumerate(pending) if at >= index and at not in done_at]
                            next_call = 0
                            raise PauseRequested() from None
                        if together is not None:
                            in_tool = False
                            for at, result in zip(segment, together):
                                call = pending[at]
                                batch_ok = batch_ok and result.ok
                                handed_off = handed_off or result.handed_off
                                tool_calls += 1
                                messages.append({"role": "tool", "tool_call_id": call.id, "content": result.content})
                                guard.record(call.name, call.arguments, result.ok, result.content)
                            index = segment[-1] + 1
                            continue
                        in_tool = rejoin_batch
                    executed = index + 1
                    index += 1
                    if self.report_requested():
                        batch_ok = False
                        messages.append({"role": "tool", "tool_call_id": call.id,
                                         "content": "Not executed: the lead requested a report."})
                        continue
                    if self.should_cancel():
                        batch_ok = False
                        messages.append({"role": "tool", "tool_call_id": call.id,
                                         "content": "Not executed: the task was cancelled."})
                        continue
                    if executed > self.limits.max_tool_calls_per_step:
                        batch_ok = False
                        content = (f"Not executed: at most {self.limits.max_tool_calls_per_step} tool calls run "
                                   "per response. Repeat this call in your next response if it is still needed.")
                        messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                        continue
                    rejoining = in_tool
                    in_tool = True
                    tool_result = self._execute(call, tool_ctx, rejoining=rejoining)
                    in_tool = False
                    content, ok = tool_result.content, tool_result.ok
                    batch_ok = batch_ok and ok
                    handed_off = handed_off or tool_result.handed_off
                    tool_calls += 1
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                    guard.record(call.name, call.arguments, ok, content)
                pending = []
                # Several single reads in a row cost a model call each; remind once or twice per run.
                batch = locals().get("last_batch") or []
                read_only = len(batch) == 1 and getattr(self.registry.get(batch[0]), "read_only", False)
                single_reads = single_reads + 1 if read_only else 0
                if single_reads >= 3 and nudges < 2 and self.settings.team.batch_nudge:
                    nudges, single_reads = nudges + 1, 0
                    messages.append({"role": "user", "content": (
                        "[Studio] Several single reads in a row. Put independent reads, searches and fetches in one "
                        "response, or use read_files for several files: they run together and save a model call each.")})
                    self.emit("batch_nudge")
                if handed_off and batch_ok and not self.should_cancel() and not take_inbox():
                    status = "dispatched"
                    break
                if self._interrupted():
                    continue
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
        except PauseRequested:
            control.frames[self.name] = {
                "messages":messages, "step":step, "tool_calls":tool_calls, "elapsed":elapsed + time.monotonic()-started,
                "usage":usage.model_dump(), "context":asdict(ctx_manager), "guard":guard.snapshot(),
                "warned":warned, "recoveries":recoveries, "empty_answers":empty_answers,
                "pending":[c.model_dump() for c in pending], "next_call":next_call, "in_tool":in_tool,
                "batch_ok":batch_ok, "handed_off":handed_off, "processes":self.sandbox.command_sessions.checkpoint(),
                "resumed":resumed, "compactions_before":compactions_before,
            }
            raise
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
            browser, self.sandbox.browser_session = getattr(self.sandbox, "browser_session", None), None
            if browser is not None:
                browser.close()
            self.sandbox.command_tmpdir = None
            shutil.rmtree(tmpdir, ignore_errors=True)
        result = AgentResult(agent=self.name, status=status, answer=answer, error=error, steps=step,
                             tool_calls=tool_calls, usage=usage, duration_s=round(elapsed + time.monotonic() - started, 2),
                             compactions=ctx_manager.compactions - compactions_before, resumed=resumed)
        self.emit("end", status=status, answer=answer, error=error, steps=step, tool_calls=tool_calls,
                  prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
                  duration_s=result.duration_s)
        return result

    # ------------------------------------------------------------------
    def _phase(self, call: ToolCall) -> int:
        tool = self.registry.get(call.name)
        return tool.phase if tool is not None else 0

    def _ordered(self, calls: list[ToolCall], assistant: dict) -> list[ToolCall]:
        """Hand-offs start after the response's other calls, all together; calls of later phases follow.

        Within one response the model sees no result before all calls finish, so no call can depend on a
        hand-off's outcome; running plan updates, writes and reads first means the hand-offs see them. The
        assistant message lists the calls in the same order, so every result follows its call.
        """
        ordered = sorted(calls, key=self._phase)
        if [c.id for c in ordered] != [c.id for c in calls] and assistant.get("tool_calls"):
            rank = {c.id: n for n, c in enumerate(ordered)}
            assistant["tool_calls"] = sorted(assistant["tool_calls"], key=lambda row: rank.get(row.get("id"), 0))
        return ordered

    def _segment(self, pending: list[ToolCall], index: int) -> list[int] | None:
        """Positions to run together from ``index``: a response made only of reads, or the run of hand-offs."""
        if index == 0 and len(pending) > 1 and all(getattr(self.registry.get(c.name), "read_only", False)
                                                    for c in pending):
            return list(range(len(pending)))
        if self._phase(pending[index]) != 1:
            return None
        end = index
        while end < len(pending) and self._phase(pending[end]) == 1:
            end += 1
        if end - index < 2 or end > self.limits.max_tool_calls_per_step:
            return None
        return list(range(index, end))

    def _run_together(self, calls: list[ToolCall], ctx: ToolContext, *, rejoining: bool = False) -> list[ToolResult] | None:
        """Run reads, or the hand-offs of one response, concurrently when allowed; results keep call order.

        Codex dispatches tools that declare parallel support under a shared lock and
        everything else exclusively; here the tool decides per batch from the arguments.
        When a pause reaches some of the calls, the finished ones are returned in
        :class:`BatchPaused` so that only the others run again on resume.
        """
        if len(calls) < 2 or len(calls) > self.limits.max_tool_calls_per_step or self.should_cancel():
            return None
        if any(call.parse_error for call in calls):
            return None
        tools = [self.registry.get(call.name) for call in calls]
        if any(tool is None for tool in tools):
            return None
        if all(tool.read_only for tool in tools):
            label = "reads"
        else:
            # Hand-offs of different tools (delegate_task, review_loop) may start together.
            if any(tool.phase != 1 or tool.concurrent is None for tool in tools):
                return None
            try:
                if not tools[0].concurrent([(call.name, call.arguments) for call in calls]):
                    return None
            except Exception:  # noqa: BLE001 - a failing predicate just means "one after another"
                return None
            label = calls[0].name if len({call.name for call in calls}) == 1 else "handoffs"
        self.emit("parallel", tool=label, count=len(calls))
        # Reads share a small pool. Hand-offs get one thread each: one may wait for another to finish,
        # so none may queue behind another for a thread.
        workers = min(len(calls), 6) if label == "reads" else len(calls)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"{self.name}-tools") as pool:
            futures = [pool.submit(self._execute, call, ctx, rejoining=rejoining) for call in calls]
        done: dict[int, ToolResult] = {}
        paused = False
        for index, future in enumerate(futures):
            try:
                done[index] = future.result()
            except PauseRequested:
                paused = True
        if paused:
            raise BatchPaused(done)
        return [done[index] for index in range(len(calls))]

    def _metered(self, response: LLMResponse, seconds: float, purpose: str) -> None:
        """One model operation for the task meter (duration and token usage, per OpenTelemetry GenAI)."""
        usage = response.usage
        self.emit("model_call", seconds=round(seconds, 3), model=self.settings.model, purpose=purpose,
                  prompt_tokens=usage.prompt_tokens, cached_tokens=usage.prompt_cache_hit_tokens or 0,
                  output_tokens=usage.completion_tokens, reasoning_tokens=usage.reasoning_tokens or 0,
                  finish=response.finish_reason)

    def _interrupted(self):
        return self.interrupt is not None and self.interrupt.is_set()

    def _execute(self, call: ToolCall, ctx: ToolContext, *, rejoining=False) -> ToolResult:
        preview = {k: (v if not isinstance(v, str) or len(v) <= 300 else v[:300] + "…") for k, v in call.arguments.items()}
        if not rejoining:
            self.emit("tool_call", tool=call.name, call_id=call.id, arguments=preview)
        began = time.monotonic()
        if self._interrupted():
            self.inbox.mark_interrupted(self.name)
            result = ToolResult.failure("Interrupted by a new message; the tool was not started.")
        elif call.parse_error:
            result = ToolResult.failure(f"Tool call rejected: {call.parse_error}. The arguments were not valid JSON — "
                                        "possibly cut off because the call was too large. Split the work into smaller calls.")
        else:
            result = self.registry.dispatch(call.name, call.arguments, ctx)
            if self._interrupted() and not result.ok and (result.meta.get("cancelled") or
                    "cancelled" in result.content.lower() or "interrupted by a new message" in result.content.lower()):
                self.inbox.mark_interrupted(self.name)
                result = result.model_copy(update={"content":"Interrupted by a new message.\n" + result.content})
        seconds = round(time.monotonic() - began, 3)
        content, ok = result.content, result.ok
        if len(content) > MAX_TOOL_RESULT_CHARS:
            content = content[:MAX_TOOL_RESULT_CHARS] + f"\n…[truncated: {len(content) - MAX_TOOL_RESULT_CHARS} more characters]"
        if not ok and not content.startswith(("Tool call rejected", "Error")):
            content = "Error: " + content
        report = result.meta.get("browser_report") if call.name == "run_browser" else None
        self.emit("tool_result", tool=call.name, call_id=call.id, ok=ok, preview=content[:600], seconds=seconds,
                  **({"browser_report": report} if report else {}))
        return result.model_copy(update={"content":content})

    def _wrap_up(self, messages: list[dict[str, Any]], reason: str, call_model, *, required=False) -> str:
        messages.append({"role": "user", "content": WRAP_UP_INSTRUCTION.format(reason=reason)})
        try:
            response = call_model(tools_enabled=False)
        except ProviderError as exc:
            if required:
                raise
            return f"(No final report: the model request failed — {exc.code}.)"
        text = response.text.strip()
        if required and (not text or response.tool_calls or response.finish_reason == "length"):
            raise ProviderError("The requested report was empty or incomplete.", code="invalid_response")
        messages.append(response.to_assistant_message())
        return text or "(The agent stopped without writing a final report.)"
