# Architecture

miniagent is a small multi-agent collaboration system. A **group** has members
with role prompts; one member is the **lead**. A requester (the human) posts a
task in the group chat; the lead plans, delegates pieces to members, may ask other members to review, asks the requester when a decision is theirs,
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

**`team.py`** runs one task against a snapshot of the group. All actors use the
same loop and base tools. A coordinating lead also has `delegate_task`. A
directly addressed member works alone. A delegation publishes the lead's edits,
checks out the project for the member, publishes the member's changes, and
returns its report, the changed paths and any missing expected files. The next
task starts with the same group's conversation, current project manifest,
recent assignments and explicitly approved role knowledge.

**`workspace.py` and `folders.py`** maintain private checkouts and the shared
project. A user can bind an external folder. At task start Studio imports
changed files. At completion it compares hashes from the last synchronization,
publishes changed outputs and preserves concurrent external edits. A different
generated version is written alongside an external edit as `.miniagent-conflict`.
Symlinks, VCS directories, caches and files over 25 MB are excluded. These
local workspaces separate files; shell and Python execute as the local OS user.

The header's workspace selector shows the resolved location for the current room. Without a binding it is `groups/<room>/project` under the runs directory. Selecting a folder persists a room preference. Each bound location has a private checkout and sync baseline, and switching back restores the corresponding checkout. Existing bindings keep their previous storage layout. On completion only paths published by this task are exported. Sync operations are serialized so groups sharing a folder can detect a previous publisher's changes. The picker uses macOS's folder dialog and [Tk's directory chooser](https://github.com/python/cpython/blob/v3.14.0/Lib/tkinter/filedialog.py) on other desktop platforms. A path can also be entered directly.

A conversation created by the Super Agent inherits its explicitly bound folder at creation. When the assistant uses Studio storage, each new conversation gets its own internal project. New assistant-created groups start pinned. Later dispatch to an existing room uses that room's own settings. Older assistant-created groups with untouched preferences receive the new pin default on startup. Existing user changes are preserved. This follows the earlier Studio [channel creation and workspace selection](https://github.com/Fei-stack-wq/maie5221-NLP-FinalProject/blob/57e6a858e9bb12838d4c19e247ba8bdafa2827b5/miniagent/studio_channels.py#L195-L255) and [pin preference regression](https://github.com/Fei-stack-wq/maie5221-NLP-FinalProject/blob/57e6a858e9bb12838d4c19e247ba8bdafa2827b5/tests/test_studio_channels.py#L42-L62).

**`studio.py`, `server.py`, `web/`** expose one service to the UI, CLI and
assistant tools. A private chat is a one-member group with independent files and
history. In a group or DM, `room_turn.py` gives the addressed role the room's
recent dialogue, published changes, named members, recorded evaluation, active task
and pending question. A text response appends two conversation events. An explicit
model action can commission work, steer the same running task, or answer the exact
pending question. A stale target leaves a visible reply. No worker workspace is
acquired for a conversation reply. Turns in one room are ordered independently of
other rooms. This restores the distinction made in the earlier Studio
`studio_room_reply.py` and uses the conversation-level handoff pattern illustrated
by the [Agents SDK routing example](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/routing.py). Explicitly queued tasks
keep their own attachments and options, and start in FIFO order. On restart,
orphaned running tasks are recorded as interrupted, then queued work resumes.
One writer process owns each data directory. The server validates its loopback
Host, same-origin Origin and per-instance token for mutations. The preview has
a separate origin. The desktop launcher picks available ports at startup.

**`roles.py`, `attachments.py`, `studio_tools.py`** implement three routes for
information. The library stores reusable definitions and append-only knowledge
revisions. An approved entry is visible to the same role either in its originating
room or generally, according to its scope. Original uploads are content-addressed
and only attachment references sent to a conversation are available to its ordinary
members. The user's unified assistant can explicitly dispatch across rooms, inspect
a dispatched task and read a published group file. Role prompts, run options and
the active roster are resolved when work is dispatched.

**`evaluation.py`** freezes per-task changed files into `artifacts.zip`. Exists,
contains and command checks score a temporary checkout. The record includes each
outcome and the command exit code. The user can inspect the score, download the
task snapshot, or export the current project together with the complete group
transcript (`chat.md` and `chat.jsonl`). Asking for changes sends another
ordinary message. There is no separate approval engine that can reverse a model
result. The separation of execution and scoring follows Inspect AI's
[scorer interface](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/src/inspect_ai/scorer/_scorer.py).
Settings and the jade palette were adapted from this repository's
[Studio integration](https://github.com/Fei-stack-wq/maie5221-NLP-FinalProject/tree/530df33).

Under `runs/`, each `groups/<gid>/` owns the public `chat.jsonl`, `project/`,
`agents/<id>/workspace`, journals, and `tasks/<tid>/` records, traces and snapshots.
Library roles live in `roles/<id>/`. Original uploads live in `uploads/<sha256>/`.
The legacy signature ledger is outside this execution path.

**Metrics** — `task.json` records status, duration, steps, tool calls,
delegations, questions and provider-reported tokens for the lead and each
assignment; `python -m miniagent report` aggregates completion rate, mean/p95
duration and tokens.

## Unified assistant conversations

The sidebar assistant, groups and DMs enter the same model-driven room turn. A direct answer appends chat events. The explicit queue control still submits work. For the assistant, the evidence also includes library roles and room rosters. Ordinary follow-ups have access to the assistant's own task evidence and its outgoing dispatch receipts. Other rooms' conversations and independently commissioned tasks stay outside this projection.

After a commission the model has local work tools as well as directory, role creation, group creation and dispatch tools. It can handle the work, reuse a role's DM or choose a team. Both the room router and the worker see the relevant directory and conversation context. This adapts the [OpenAI Agents SDK agent-as-tool mechanism](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/src/agents/agent.py#L646) and its [nested result test](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/tests/test_agent_as_tool.py#L558). Studio returns an asynchronous receipt identifying a durable task; the destination's own execution record supplies its progress and delivery.

- Each new dispatch records the source room and parent task in the target's queued and running records. Pre-routing history can be recovered from successful `start_task` tool results in the assistant's own trace. The evidence includes running or queued dispatches and the four most recent terminal dispatches in detail. Earlier dispatches have a compact catalog and their original receipts can be read on demand. A published file excerpt resolves against that specific task's evidence.
- A progress question can be answered even after the dispatching assistant finishes. Changes and answers identify an observed running task. Multiple eligible targets require an explicit task ID in the model action. Delivery rechecks the captured execution and pending question, so a late answer cannot land in the next task. Forwarding creates a visible receipt in the assistant conversation.
- A standalone roster request can call `change_group_members(group_id, add_role_ids?, remove_member_ids?, lead?)` from chat, without creating a task. A commissioned assistant can use the same operation as part of organizing a delivery. The service applies the delta under the same lock as the group editor, validates the complete roster and preserves member notes. A repeated change has no side effects. Removing the lead requires a remaining lead. A running task keeps its original team; later tasks use the revised roster.

Offline coverage lives in `tests/test_assistant_room.py`, `tests/test_studio_fusion.py` and `tests/test_ui_smoke.py`.

## Studio workbench

The workbench opens existing rooms through the normal composer. Opening task details, traces or publications only performs reads. The overview follows the [integrated Studio workbench, tool disclosure and Markdown reader](https://github.com/Fei-stack-wq/maie5221-NLP-FinalProject/tree/530df33/miniagent/web). Its data comes from the bounded runtime.

- `GET /api/tasks` supplies the workbench and filters. `GET /api/tasks/<id>?view=summary` omits the large trace. The root identifies the task's primary agent. Each recorded `delegation` adds one branch, including repeat assignments to the same member. The subsequent `report` from that assignee supplies its outcome. A terminal task without a matching report shows that the report is missing.
- `GET /api/tasks/<id>/tools?cursor=<byte offset>` returns recorded calls and results, the next cursor and a more-pages flag. The chat disclosure matches results by actor and call ID in trace order. The model runtime determines which tools are called. The UI displays the stored argument and output previews.
- Report and publication cards link to `GET /api/tasks/<id>/files/<path>?sha256=<published digest>`. Task snapshots supply historical bytes. For older records without a snapshot the current file can supply the bytes if its digest matches. Missing revisions return 404. The room file panel opens the current project version.
- The reader uses the bundled markdown-it with raw HTML disabled. Document links and raster images resolve inside the originating task or project. Escape closes the reader and Back returns to the preceding document.

## Dispatch receipts in the assistant chat

When the assistant creates a room, the room record saves the originating group and task. When it starts a task, the queued record saves the same provenance before the worker can run. The assistant chat receives `assistant_group_created` and `dispatch_sent` events at those commits. The receipt links to the actual room and task, including work queued behind another run.

By default a successful `start_task` handoff ends the assistant turn with status `dispatched`. The child runs independently. If the assistant has other work, it can choose `after_dispatch="continue"`. When it needs the child's result for synthesis, `"await_result"` waits on a completion signal that also wakes on cancellation or shutdown. The agent can still check status on request. A tool error in the same batch or steering that arrived during a call returns control to the model.

The child posts `dispatch_finished` to the originating chat after recording the outcome, snapshot and evaluation. The notification carries the final answer, actual status, warnings and published file revisions. File cards use the child's task ID and digest. Failed and cancelled work has its own outcome. A handoff counts separately from a completed delivery in group metrics.

A notification ID combines the event kind with the target room or task ID. Each source chat checks the ID under its append lock. Startup replays committed target records and can recover older task origins from the source assistant's successful dispatch trace. A crash between dispatch and notification, or reopening the chat, therefore does not require another model turn.

The batch handoff follows the [OpenAI Agents SDK tool termination path](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/src/agents/run_internal/turn_resolution.py#L808-L831). The separate child start and outcome signals follow the [Temporal Python SDK child completion handler](https://github.com/temporalio/sdk-python/blob/e64f9dd3b3b513da6676caf5c1a6cbbb36119503/temporalio/worker/_workflow_instance.py#L985-L1020). Studio implements these semantics using its existing group records, chat log and worker condition. Offline API, worker, restart and browser regressions are in `tests/test_dispatch_notices.py`.

## Streaming public chat replies

The room router requests streaming on both its initial model call and its read-only continuation after an evidence lookup. Task leads and members use the same public content stream. The existing POST still returns the committed route and result to API clients. Opening a room in the browser also subscribes to `GET /api/groups/<id>/chat-stream` using Server-Sent Events. Visible model text appears inside the timeline while the POST or worker is still running. Thinking tokens and tool arguments stay out of the reply bubble.

Each group holds its own transient drafts, one per task and actor plus one for the current conversation turn. A subscriber receives the current text on connection and new fragments thereafter. A failed provider attempt drops its draft. When the chat log commits progress or a final message, the stream names the new sequence number. The browser fetches those durable events and replaces the draft with the committed Markdown. Refreshing the page can reattach to an active draft. Workbench links and dispatch notifications still come from the chat log.

This follows the separation between incremental model events and committed items in the [OpenAI Agents SDK streaming implementation](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/src/agents/result.py#L1005-L1080) and its [terminal output regressions](https://github.com/openai/openai-agents-python/blob/91fb508a8931016efa793b033147d08cd20308cf/tests/test_streamed_terminal_output_backfill.py#L62-L118). Studio reuses its existing room log for recovery. `tests/test_chat_stream.py` holds the model stream open until Chromium has rendered the first segment, including across a refresh.

## Verification

* `python -m unittest discover -s tests -t .` — offline suite (scripted model):
  loop termination and wrap-up, the re-reading regression, protocol validity,
  steering, cancellation, compaction, delegation/publishing/isolation,
  questions, budgets, @mentions, cross-task context, HTTP API and preview origin.
* `python -m miniagent demo` — offline end-to-end team run.
* Live runs of the Mid_Test scenarios: see [mid-test-status.md](mid-test-status.md).
