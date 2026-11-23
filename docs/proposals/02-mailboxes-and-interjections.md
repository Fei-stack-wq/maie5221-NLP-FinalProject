# Proposal 02 — Agent mailboxes, interjections and lead decisions during hand-offs

Status: implemented for offline validation; real-model comparison and blind deliverable review pending · 2026-11-22 · base: `jinyuwu/model-driven-studio` at `3a9284c`
Depends on: nothing. Proposal 03 builds on it.

## Problem

Users of Codex or Claude Code can interject at any time: a message to a running agent is delivered
at the next model/tool boundary, and can interrupt the current turn. Studio is coarser:

- **One broadcast list.** A message the room router classifies as `change_current_task`
  (`room_turn.py`, `STEER`) is appended to `HumanChannel._messages` (`team.py`,
  `HumanChannel.receive`). Every working agent reads unread entries at its next step
  (`HumanChannel.take`, called from `Agent.run` → `take_inbox`). There is no addressing.
- **The lead is out of the loop during a hand-off.** `delegate_task` is synchronous, so the lead's
  loop is suspended inside the tool call. A message reaches the lead only when the member finishes —
  in the Mid_Test runs, single assignments ran 11–19 minutes. The lead cannot re-plan, redirect or
  stop the member in between.
- **No interrupt.** Delivery waits for the current model call and tool to finish (a failing browser
  check took up to 30 s before the 5 s timeouts; shell commands can run up to their timeout). Only
  pausing or cancelling the whole task acts immediately.
- **No channel between roles** at all (see proposal 03).

## Goals

- A message can be addressed to one agent, all working agents, or the lead.
- Urgent messages from the user or the lead interrupt the recipient's current model call or tool.
- The lead can act on a message while one of its hand-offs is still running.
- One mechanism that proposal 03 (check-ins, teammate messages) and Studio notices reuse.

Non-goals: agents running without a lead; members interrupting each other.

## Design

### Mailboxes

`HumanChannel` keeps questions/answers (`ask`, `pending`, answer routing) and gains per-agent
mailboxes instead of the single `_messages` list:

```python
@dataclass
class Mail:
    id: str               # "m-…"
    seq: int              # chat event seq of the "mail" event
    sender: str           # "user", "studio", or an agent id
    to: list[str]         # agent ids; ["*"] = every agent working on the task
    kind: str             # "steer" | "note" | "notice" | "decision"
    text: str
    urgent: bool = False
```

- `post(mail)` appends a `mail` chat event (sender, recipients, kind, urgent) and queues it per
  recipient. `take(agent)` returns that agent's undelivered mail, formatted by sender:
  `[Message from the requester]`, `[Message from <Lead name>]`, `[Note from <Member name>]`,
  `[Studio notice]`. The existing `note()` (budget notices) becomes `post(kind="notice")`.
- Delivery stays at the next step boundary (`Agent.run` → `take_inbox`), plus the interrupt below.
  A `mail_delivered` trace event records when each recipient read it (latency metric).
- `snapshot()`/`restore()` include queued mail, so pause/resume keeps it.
- A broadcast to `*` reaches agents that start later in the task through their context
  (as today via `start_cursor`), not as mail.

### Addressing from the chat

- `STEER` (`change_current_task`) gains `recipients` (member IDs; empty = everyone working) and
  `urgent` (boolean). The router fills `recipients` from leading @mentions (`mention()` in
  `studio.py` already resolves `@id`/`@Name`).
- Composer: while a task runs, a small "发给：全体 / @dev" indicator derived from the mention, and an
  "立即打断" (interrupt) toggle. The chat shows a receipt per recipient: "已送达 dev（下一步）" or
  "已打断 dev" when read.

### Interrupts

- Each agent run gets an `interrupt: threading.Event` (owned by `TeamTask`, keyed by agent id),
  separate from task cancellation.
- `Agent.run`: the streaming model call's `should_cancel` also checks the interrupt; an interrupted
  call is discarded (no partial assistant message is appended, the step is not counted, history stays
  valid). Tools receive a `ToolContext.should_cancel` that includes the interrupt. `run_shell` and, on
  macOS/Linux, `run_python` (both via `run_posix_argv` → `_drain`) and `BrowserSession._wait` already
  poll it and stop; the non-POSIX `run_python` path (`subprocess.run` in `tools.py`) only checks before
  starting and needs to move to the same polling loop. The tool result reads
  `Interrupted by a new message` so the tool call still has its tool message.
- After an interrupt the agent takes its mail immediately and continues.
- Who may interrupt: the user and the lead. Members never interrupt (proposal 03 notes are
  non-interrupting). At most one interrupt per recipient per 60 s; extra urgent mail is queued.

### Lead decisions while a hand-off runs ("lead consult")

When mail for the lead arrives while the lead is suspended in `delegate_task` or `review_loop`
(known from `TeamTask.inflight`), Studio runs one short **consult** call for the lead instead of
holding the mail until the member finishes:

1. Input: a copy of the lead's current conversation (the lead runs with an `AgentSession` so its
   messages are readable), with synthetic tool results `"(still running: <member> is working on
   this)"` for the pending calls so the history validates, plus the new mail and the members'
   recent progress notes.
2. The lead model answers with JSON (text, no tools — DeepSeek refuses named tool choice in thinking
   mode): `{"action": "forward" | "forward_urgent" | "stop_member" | "reply" | "hold", "to": [...], "text": "..."}`.
   - `forward` / `forward_urgent`: post the text to the member(s), optionally interrupting.
   - `stop_member`: urgent mail asking the member to stop and report now.
   - `reply`: answer the user in the chat.
   - `hold`: keep the mail for the lead's next step.
3. Unparsable answer → `forward` the original message to the running member(s) (safe default) and
   `hold` a copy for the lead.
4. The decision is posted as a `decision` mail to the lead itself, so when the lead's loop resumes it
   sees `[While <member> was working you decided: …]` and stays consistent.
5. Limits: one consult at a time per task, at most 10 per task (3 in fast mode). Consults use the
   lead's provider and settings; their tokens count in the meter (`purpose="consult"`).

Proposal 03 sends member check-ins through the same consult.

### Studio notices

Budget notices (`TaskMeter.notify`) and future silence prompts become `kind="notice"` mail.

## Changes by file

| File | Change |
| --- | --- |
| `miniagent/team.py` | `Mail`, per-agent queues in `HumanChannel` (`post`, `take`, snapshot/restore); `TeamTask.interrupts`; lead runs with an `AgentSession`; `consult_lead()`; `_event` records `mail_delivered`. |
| `miniagent/agent.py` | Interrupt-aware streaming call (discard partial, uncounted step) and tool context; take mail right after an interrupt. |
| `miniagent/tools.py`, `shell_tool.py`, `browser_tool.py` | `ToolContext.should_cancel` combines task cancel and agent interrupt; tool result text `Interrupted by a new message`. |
| `miniagent/room_turn.py` | `STEER` schema: `recipients`, `urgent`. |
| `miniagent/studio.py` | `post()` passes recipients/urgent to `HumanChannel.post`; `state()["live"]` exposes queued/delivered mail per agent for receipts. |
| `miniagent/meter.py` | `purpose="consult"` counted; interrupts and mail latency in the snapshot. |
| `miniagent/web/app.js`, `index.html`, `app.css` | Recipient indicator, 立即打断 toggle, delivery receipts, consult decision cards. |
| `docs/architecture.md` | New row with upstream links. |

Switches: `MA_MAILBOXES` (targeting + receipts), `MA_INTERRUPTS`, `MA_LEAD_CONSULT` (all default on
after measurement; off = today's broadcast).

## Tests (offline, through `Studio.post` / the HTTP API)

1. `@dev` steer during a task reaches only dev; a plain steer reaches all working agents; agents that
   start later see it in context, not as mail.
2. Urgent steer to a member running `run_shell sleep 30` is read within 2 s; the tool result says
   "Interrupted by a new message"; the history validates with `DeepSeekProvider._validate_history`.
3. Interrupt during a streaming model call discards the partial answer and does not count the step.
4. User steer while the lead waits in `delegate_task` triggers one consult; each action
   (`forward`, `forward_urgent`, `stop_member`, `reply`, `hold`) has a test; the lead later sees the
   `decision` mail; an unparsable consult forwards and holds.
5. Interrupt rate limit; consult limit; fast-mode limits.
6. Pause during a consult and during an interrupted tool; resume delivers queued mail once.
7. Playwright: receipts and the interrupt toggle in the chat.

## Measurement and acceptance

- Latency from user message to agent read: p95 ≤ one step (~20 s) normal, ≤ 3 s urgent (record via
  `mail_delivered`).
- Lead reaction while a member runs: consult decision within ~20 s.
- `bench` + blind quality check on the Task 1s: no new failures; scripted interjections (a mid-task
  requirement change sent 5 minutes in) are reflected in the deliverable.

## Risks

- **History corruption after interrupts** — discard partial responses; validate with the provider
  validator in tests.
- **Interrupt storms or loops** — only the user and the lead interrupt; rate limit.
- **Consult misjudgement** — decisions are visible cards; the user can override; unparsable →
  conservative forward.
- **Cost** — one short call per consult, counted by the meter.

## Upstream (read before implementing; cite pinned links)

Codex multi-agent tools (Apache-2.0), all at
`openai/codex@44848822b1a58fa40cdcd89ec3161c757735cfd0`, `codex-rs/core/src/tools/handlers/multi_agents_spec.rs`:
- [`send_input` with `interrupt=true`, L193-L194](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L193-L194) — redirect a running agent immediately.
- [`send_message`, L222-L223](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L222-L223) — delivered promptly without triggering a new turn (our non-urgent mail).
- [`followup_task`, L255-L256](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L255-L256) — delivery "at message boundaries while sampling, or after the pending tool call completes".
- [`wait_agent` mailbox, L305-L306](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L305-L306) and [`interrupt_agent`, L370-L371](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L370-L371).
- [`AgentControl::send_input`, control.rs L138-L168](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/agent/control.rs#L138-L168) (start-or-steer a turn) and its tests [L909-L922, L985-L1007](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/agent/control_tests.rs#L909-L1007).

Difference to adopt consciously: in Codex the parent keeps running while children work, so it can
send input itself; Studio's lead is suspended, so the consult call stands in for the parent's turn.
