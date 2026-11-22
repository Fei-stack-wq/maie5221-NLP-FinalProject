# Architecture

miniagent is a small multi-agent collaboration system. A **group** has members
with role prompts; one member is the **lead**. A requester (the human) posts a
task in the group chat; the lead plans, delegates pieces to members, has their
work checked by other members, asks the requester when a decision is theirs,
and finishes with a final answer. Files are the team's shared output.

```
browser ── server.py (API + UI, preview origin) ── studio.py (one task thread per group)
                                                       │
                                                  team.py  TeamTask
                                                   │   lead Agent ── delegate_task ──► member Agent
                                                   │   ask_human ◄──► HumanChannel ◄── requester messages
                                                   │   workspace.py  private workspaces ⇄ team project
                                                  agent.py  bounded Reason–Act–Observe loop
                                                   ├─ context.py     compaction (Pi-style)
                                                   ├─ loop_guard.py  stuck detection (OpenHands-style)
                                                   ├─ tools.py …     files, Python, shell, browser, web research
                                                   └─ providers.py   DeepSeek / scripted
```

## Why the old runtime was replaced

The previous runtime (kept at git ref `refs/backup/pre-rebuild-20261122`) had
grown to ~1.5 MB of Python around a host-enforced review bureaucracy: signed
reviews, frozen candidates, adjudication, probe receipts, private drafts and
regex gates on questions. 1,299 unit tests passed, yet most real group tasks
failed or were cancelled. Three root causes, all visible in `runs/`:

1. **Unbounded loops.** `max_steps = 0` meant "unlimited" and was the
   default. Run `studio-20261121-043842-6fc84dad0251` (investment proposal)
   ran 2 h 43 min: the lead made **4,436 model calls and 8,586 `run_shell`
   calls** until the user cancelled it.
2. **Amnesia by design.** In its "coordinate" phase the lead was sent a
   *compact* request — system prompt, goal, a large host state dump and only the
   last four turns (`deque(maxlen=4)`). Prompt size stayed flat at ~16k tokens
   per call while the lead re-read the same files with `grep`/`sed` forever: it
   could not remember what it had just read. No identical-call detector fired,
   because each command differed slightly.
3. **Host gates instead of collaboration.** Deliveries were blocked by review
   state machines, file-type allow-lists and a heuristic "credential" scanner
   (it rejected an `sk-hynix-…` news URL as an API key). Leads repeatedly handed
   the same check to members who were not allowed to sign it.

The rebuild keeps what worked — the DeepSeek client, the sandbox and the tool
implementations — and replaces the agent loop, the team protocol, persistence,
the server and the UI.

## Invariants

1. **Every loop terminates.** Agent steps are always finite (`Limits.max_steps`
   1–1000; `0` is rejected). The lead has `TeamLimits.lead_max_steps` and
   `max_delegations`; near-identical repeated assignments are refused. When a
   budget runs out or the loop guard fires, the model is asked once more with
   `tool_choice: "none"` and must write its report — the caller always gets an
   answer and an honest status (`completed`, `max_steps`, `stuck`, `budget`,
   `cancelled`, `error`).
2. **No silent forgetting.** The whole conversation is sent each step. Past
   `compact_at_tokens` (default 110k), old large tool outputs are trimmed and,
   if still needed, the older span is replaced by one structured checkpoint
   summary. The system prompt and the task message are pinned.
3. **Valid protocol history.** Every assistant tool call gets exactly one tool
   message (refused, invalid, over-limit calls included). Tests validate
   histories with the provider's own validator.
4. **Isolation with one explicit channel.** Each agent works in its own
   sandbox directory (path escapes are refused). Files reach other agents only
   through the team project: checkout before an assignment, publish when it
   ends. Delegation is sequential, so publishes never race.
5. **The requester decides, the team executes.** `ask_human` blocks the
   asking agent until the requester answers in the chat. Messages sent while a
   task runs are delivered to the working agents at their next step. In
   autonomous mode the question tool is not offered.

## Upstream designs adopted

Per the project convention, each mechanism below links to the upstream source
read before implementing it (pinned commits), what was adopted, and what was
changed.

| Mechanism | Upstream | Adopted | Changed for this project |
| --- | --- | --- | --- |
| Stuck detection (`loop_guard.py`) | OpenHands SDK [`stuck_detector.py`](https://github.com/OpenHands/software-agent-sdk/blob/fb75ae907585c1634a85fd5168a795dd48eb4d65/openhands-sdk/openhands/sdk/conversation/stuck_detector.py), thresholds in [`types.py`](https://github.com/OpenHands/software-agent-sdk/blob/fb75ae907585c1634a85fd5168a795dd48eb4d65/openhands-sdk/openhands/sdk/conversation/types.py#L136-L161), tests [`test_stuck_detector.py`](https://github.com/OpenHands/software-agent-sdk/blob/c8a65f0db7c4e5b455a124989c3a6a0a79fcd9e9/tests/cross/test_stuck_detector.py) (MIT) | Same thresholds: identical action+observation ×4, identical failing action ×3 (nudge, stuck on the next), alternating pattern over 6; window resets on a new user message; nudge once per streak. | Added a *re-reading* rule (≥8 of the last 12 observations already seen → nudge; ≥13 of 16 after the nudge → stuck) for the amnesia loop above; `poll_process` waits are exempt. A stuck run ends with a tool-less report instead of a STUCK state. |
| Compaction (`context.py`) | Pi [`compaction.ts`](https://github.com/earendil-works/pi/blob/adfc14461aa11adbf486efad9b0ede2dc0dab06a/packages/coding-agent/src/core/compaction/compaction.ts): settings [L126-L130](https://github.com/earendil-works/pi/blob/adfc14461aa11adbf486efad9b0ede2dc0dab06a/packages/coding-agent/src/core/compaction/compaction.ts#L126-L130), `findCutPoint` [L446-L500](https://github.com/earendil-works/pi/blob/adfc14461aa11adbf486efad9b0ede2dc0dab06a/packages/coding-agent/src/core/compaction/compaction.ts#L446-L500), summary prompts [L507-L581](https://github.com/earendil-works/pi/blob/adfc14461aa11adbf486efad9b0ede2dc0dab06a/packages/coding-agent/src/core/compaction/compaction.ts#L507-L581) (MIT) | Threshold-triggered compaction; keep recent tokens verbatim; cut only at user/assistant messages, keeping a trailing tool batch with its call; structured checkpoint format (Goal / Constraints / Progress / Decisions / Next steps / Context); *update* the previous summary instead of re-summarizing it; a length-truncated summary is rejected. | System prompt and task are pinned. A cheap tool-output trim runs first (idea from DeepSeek harness [`compaction-tool-result-pruner`](https://github.com/deepseek-ai/deepseek-harness/blob/ca33fd6843b205e279c7ccb6cb73a472c78f5760/packages/compaction/compaction-tool-result-pruner/src/index.ts)) only when the threshold is crossed, keeping the prompt prefix cache-stable. A failed summary falls back to the trim. Added "Facts Already Established" so findings are not looked up again. |
| Turn limit and final output | OpenAI Agents SDK [`run.py` max-turns handling](https://github.com/openai/openai-agents-python/blob/3a0c89c8e4ac42fe4134695d3ca5d0275f138f06/src/agents/run.py#L1593) (MIT) | A hard turn limit per run. | Instead of raising `MaxTurnsExceeded`, one last request with `tool_choice: "none"` produces the report, so the lead always receives something usable. |
| Delegation as a synchronous tool | OpenAI Agents SDK [`Agent.as_tool`](https://github.com/openai/openai-agents-python/blob/d5287b741dfd93de3ce1625f98b342cd0c4c455e/src/agents/agent.py#L646) (MIT); AutoGen Magentic-One [orchestrator](https://github.com/microsoft/autogen/blob/98e6bba168c784cc3d8ae9491453241b10f4f5ee/python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/_magentic_one/_magentic_one_orchestrator.py#L299-L405) | The lead calls members like tools and gets their final output back; a round cap (`max_turns`) and a stall counter (`max_stalls`) bound the orchestration. | Members run full tool loops in private workspaces and publish files; the stall idea became the repeated-assignment refusal (text similarity ≥ 0.9, `max_repeat_assignments`) plus the delegation budget. No separate progress-ledger model call. |

## Components

**`agent.py`** — `Agent.run(task, context)`: messages are `[system, context+task,
…]`. Each step: cancellation/budget checks → steering messages → compaction →
model call → tool batch (≤ `max_tool_calls_per_step`, extra calls answered as
"not executed") → loop guard. Output cut off by the length limit is recovered
up to twice. Background processes started by shell commands are tracked by
process group and killed when the run ends.

**`team.py`** — `TeamTask` runs one requester message. The lead's extra tools:
`delegate_task(assignee, task, deliverables)`, `ask_human(question)`,
`remember(note)`. A delegation publishes the lead's own edits, checks out the
project into the member's workspace, runs the member, publishes the member's
changes, refreshes the lead, and returns the full report with the published
file list and any missing expected files. Every agent's first message contains
the group conversation so far (previous tasks, delegations, reports, questions,
answers, final answers), the project file list, its own long-term notes and its
recent assignments — this is how Task 2 continues from Task 1.

**`workspace.py`** — `Project.checkout` mirrors the project into a workspace;
`Project.publish` applies a workspace's adds/modifications/deletions since its
baseline and logs them. Caches (`node_modules`, `.venv`, …), VCS folders,
symlinks and files over 25 MB are skipped and reported.

**`store.py`** — plain files under `runs/groups/<id>/`: `group.json`,
`chat.jsonl` (numbered public events), `project/`, `agents/<id>/workspace|memory.md|journal.jsonl`,
`tasks/<id>/task.json|trace.jsonl`.

**`studio.py` / `server.py` / `web/`** — one task thread per group; a
requester message answers a pending question, else steers a running task, else
starts a task (`@member` addresses one member directly). The API (port N)
requires an `X-Studio: 1` header on mutations; generated files are served from
a separate preview origin (port N+1) so a generated page can use
`localStorage` but cannot reach the API.

**Metrics** — `task.json` records status, duration, steps, tool calls,
delegations, questions and provider-reported tokens for the lead and each
assignment; `python -m miniagent report` aggregates completion rate, mean/p95
duration and tokens.

## Verification

* `python -m unittest discover -s tests -t .` — offline suite (scripted model):
  loop termination and wrap-up, the re-reading regression, protocol validity,
  steering, cancellation, compaction, delegation/publishing/isolation,
  questions, budgets, @mentions, cross-task context, HTTP API and preview origin.
* `python -m miniagent demo` — offline end-to-end team run.
* Live runs of the Mid_Test scenarios: see [mid-test-status.md](mid-test-status.md).
