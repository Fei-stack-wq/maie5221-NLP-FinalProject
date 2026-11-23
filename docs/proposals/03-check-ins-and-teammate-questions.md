# Proposal 03 — Member check-ins, teammate questions and silence prompts

Status: proposed · 2026-11-22 · base: `jinyuwu/model-driven-studio` at `3a9284c`
Depends on: proposal 02 (mailboxes and the lead consult). Proposal 01 is helpful (check-ins and
questions can name a plan step) but not required.

## Problem

Members work alone for long stretches and can only talk upward at the very end.

- **Long blind runs.** In the Mid_Test Task 1 runs (22 Nov), single assignments ran with no contact:
  developer 17–18 min, QA 14–15, reporter 13–19, research 11–15. A misread brief is discovered only
  in the report, and fixing it costs another full hand-off.
- **Coordination mistakes.** In the fund run the lead started a risk review in parallel with the
  research it depended on; research had to be redone (13 min, then 15 min again).
- **Questions become re-delegations.** Members have no tool to reach the lead or a teammate. A
  fact-checker who needs a quote's source must guess or report back so the lead can start a new
  hand-off: a question costs 5–15 minutes.
- **Repeated work.** In the new-features team's software run, the developer (48 browser calls,
  6.3 min), QA (45 calls, 11.1 min) and the lead (11 calls, 2.6 min) tested the same pages; nobody
  could say "already covered". (Commit `326bf19` added "verification has an owner" prompts; this
  proposal adds the channel to coordinate it.)
- **Why it is built this way.** Hand-offs are synchronous tools (OpenAI Agents SDK `as_tool`,
  Magentic-One orchestrator). Hub-and-spoke guaranteed termination after the old runtime looped for
  hours; peer chatter was deliberately left out. The cost now outweighs that benefit.

## Goals

- Members raise decisions, blockers and deviations to the lead while they work, and get an answer
  without ending their run.
- Members can ask a teammate a factual question and get a short answer without a re-delegation.
- Members can leave a teammate a note ("the overlap check already fails; don't build on it").
- Long silent runs are prompted to check in.
- Everything is bounded and visible; no chatter loops.

Non-goals: free-form multi-agent chat; members assigning work to each other.

## Design

### 1. `check_in` (member → lead)

```json
{"name": "check_in", "parameters": {"summary": "string", "decision": "string (optional)",
 "options": ["string"], "step": "plan step id (optional)"}}
```

- Posts `kind="checkin"` mail to the lead and runs the **lead consult** from proposal 02 with the
  member's note. The consult's decision comes back as the tool result:
  `continue`, `adjust` (with instructions), or `stop_and_report`. The lead's own loop later sees the
  decision as `decision` mail, as in proposal 02.
- When to use it (member prompt): a choice between options the brief did not settle, a blocker, a
  planned deviation from the brief, or a milestone the lead asked for. Not for routine progress.
- Limits: 3 per assignment (1 in fast mode); a refused call says so and the member continues.

### 2. `ask_teammate` (member → member, question with answer)

```json
{"name": "ask_teammate", "parameters": {"member": "teammate id", "question": "string",
 "context": "string (optional)"}}
```

- Studio answers from the teammate's perspective without touching the teammate's live work:
  - It forks the teammate's latest work session (a copy of `AgentSession.messages`, like Codex
    `fork_context`), or uses the role prompt plus the group context when the teammate has no session.
  - The fork runs with **read-only tools** (`build_default_registry(read_only=True)`), at most 8 steps,
    no publish, using the teammate's provider settings (members without thinking in fast mode).
  - The answer is the tool result; a `[QA asked you … You answered …]` note goes to the teammate's
    mailbox so its real session knows what it said.
- Works whether the teammate is idle or running in a parallel batch (the fork never writes to the
  live session).
- Limits: 3 questions per assignment, 1 open question at a time per asker, answers ≤ 8 steps.
  Asking oneself or the lead is refused (the lead is reached with `check_in`).

CrewAI gives agents the same capability ("Ask question to coworker"); there the coworker runs a full
task, here the answer is a short read-only turn.

### 3. `message_teammate` (member → member, note)

```json
{"name": "message_teammate", "parameters": {"member": "teammate id", "text": "string"}}
```

- Non-interrupting `kind="note"` mail, delivered at the teammate's next step if it is working, or in
  its context at its next assignment. No answer is awaited.
- Limit 5 per assignment.

### 4. Silence prompts

- `TaskMeter` already tracks per-agent run steps and time. When a member has worked ≥ 10 min
  (5 min in fast mode) or ≥ 60 % of its steps without a `check_in`, Studio posts a `notice` mail:
  "Send a short check_in now: status, what is left, and anything the lead should decide."
- One prompt per assignment run; the meter shows it as an alert.

### 5. Lead guidance

Lead prompt: "For assignments longer than a few minutes, name check-in points in the brief (for
example 'check in after the data model, before the UI'). Teammates can ask each other questions and
leave notes; you will see them in the chat."

### Visibility

Chat cards: check-in (member → lead, with the decision), teammate question and answer (folded by
default), note. Task details show per-assignment counts. All are trace events for the meter and
`bench`.

## Changes by file

| File | Change |
| --- | --- |
| `miniagent/team.py` | Member registry adds `check_in`, `ask_teammate`, `message_teammate` (per-assignment counters on the `inflight` entry); `answer_as_teammate()` (fork session, read-only registry, 8 steps); member and lead prompt text; chat events `checkin`, `teammate_question`, `teammate_answer`, `note`. |
| `miniagent/meter.py` | Silence rule (time/step share without check-in) → `notify` the member; counts of check-ins, questions, notes. |
| `miniagent/config.py` | `TeamLimits`: `check_ins` (3), `teammate_questions` (3), `teammate_notes` (5), `silence_minutes` (10); `fast_profile` lowers them (1, 1, 2, 5). Switch `MA_TEAM_TALK`. |
| `miniagent/web/app.js`, `app.css` | Cards for check-ins, Q&A (folded), notes; meter alert text. |
| `miniagent/bench.py` | Columns: check-ins, questions, notes, re-delegations (same assignee, similar brief), browser minutes by non-owners. |
| `docs/architecture.md` | Row with upstream links. |

## Tests (offline, through `Studio.post`)

1. `check_in` mid-run → consult → `adjust` instructions returned as the tool result; the member
   continues in the same run; the lead later sees the decision; limit enforced.
2. `stop_and_report` → the member wraps up with a report.
3. `ask_teammate` to an idle teammate with a session: answer comes from a fork; the teammate's files
   and session are unchanged except the note; read-only tools only (a write attempt is refused).
4. `ask_teammate` while the teammate runs in a parallel batch: the answer arrives; the live run is
   undisturbed; the note is delivered at its next step.
5. `message_teammate` delivered at the next step; to a not-yet-started teammate → appears in its next
   assignment's context.
6. Silence prompt fires once after the threshold (fake clock), not again in the same run.
7. Fast-mode limits; pause/resume with an open question (the fork is discarded and the question
   re-asked on resume, or answered before pausing — pick one and test it).

## Measurement and acceptance (bench + blind quality check)

- Re-delegations per task go down; browser minutes spent by non-owners go down.
- Wall time not worse than today on the three Task 1s; tokens within +10 %.
- Blind quality equal or better; specifically, no work started on missing inputs (fund) and no full
  re-test rounds caused by duplicated verification (software).
- Check-ins, questions and notes stay under their limits in ≥ 95 % of assignments (if they hit the
  limits often, the limits or prompts are wrong).

## Risks

- **Chatter and cost** — hard per-assignment limits; answers are short read-only turns; everything is
  in the chat and the meter.
- **Stale answers from a fork** — the answer says when the teammate's session was last updated; the
  note to the teammate lets it correct itself.
- **The lead over-steering through consults** — consult decisions are cards the user sees; the lead
  prompt asks it to answer decisions, not to micromanage.

## Upstream (read before implementing; cite pinned links)

- CrewAI agent tools (MIT), `crewAIInc/crewAI@56c26a7fb3ece62c64de52e8b4228d15c9c81492`:
  [`AskQuestionTool`, ask_question_tool.py L8-L28](https://github.com/crewAIInc/crewAI/blob/56c26a7fb3ece62c64de52e8b4228d15c9c81492/lib/crewai/src/crewai/tools/agent_tools/ask_question_tool.py#L8-L28),
  [`BaseAgentTool._execute`, base_agent_tools.py L46-L124](https://github.com/crewAIInc/crewAI/blob/56c26a7fb3ece62c64de52e8b4228d15c9c81492/lib/crewai/src/crewai/tools/agent_tools/base_agent_tools.py#L46-L124)
  (unknown-coworker and execution errors), tests in
  [`lib/crewai/tests/tools/agent_tools/test_agent_tools.py`](https://github.com/crewAIInc/crewAI/blob/56c26a7fb3ece62c64de52e8b4228d15c9c81492/lib/crewai/tests/tools/agent_tools/test_agent_tools.py).
- Codex `fork_context` (answering from a copy of an agent's history):
  [multi_agents_spec.rs L625-L631](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L625-L631)
  and `send_message` for non-interrupting notes:
  [L222-L223](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L222-L223) (Apache-2.0).
- OpenTelemetry GenAI agent metrics for the new counters:
  [gen-ai-metrics.md L595-L886](https://github.com/open-telemetry/semantic-conventions-genai/blob/4181b1ebe55da7edb3538636c45ae64af369b5ef/docs/gen-ai/gen-ai-metrics.md#L595-L886) (Apache-2.0).
