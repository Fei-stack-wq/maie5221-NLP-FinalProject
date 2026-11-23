# Architecture

miniagent is a small multi-agent collaboration system. A **group** has members
with role prompts; one member is the **lead**. A requester (the human) posts a
task in the group chat; the lead plans, delegates pieces to members, may ask other members to review, asks the requester when a decision is theirs,
and finishes with a final answer. Each session has its own chat and project. Its members can share files across tasks in that session.

```
browser ── server.py (API + UI, preview origin) ── studio.py (one task thread per session)
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
   ends. Assignments run one after another unless the lead sends several in one
   response with disjoint declared files; publishes are serialized by the project
   lock, and a file overwritten by a concurrent publish is reported as a conflict.
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
| Continued member sessions (`AgentSession`, `TeamLimits.resume_members`) | Codex CLI multi-agent tools: [`send_input` spec](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L166-L194) and the `fork_context` default [L625-L631](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L625-L631), [`AgentControl::send_input`](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/agent/control.rs#L138-L168), tests [`send_input_errors_when_thread_missing`](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/agent/control_tests.rs#L909-L922) and [`send_input_submits_user_message`](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/agent/control_tests.rs#L985-L1007) (Apache-2.0); AutoGen [`AssistantAgent` keeps its model context between calls](https://github.com/microsoft/autogen/blob/98e6bba168c784cc3d8ae9491453241b10f4f5ee/python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py#L108-L114) and clears it only in [`on_reset`](https://github.com/microsoft/autogen/blob/98e6bba168c784cc3d8ae9491453241b10f4f5ee/python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py#L1622-L1624) (MIT) | A follow-up to an existing agent is appended to that agent's own history instead of starting a new one; a new agent starts with only its prompt unless told otherwise; the conversation lives for the duration of the parent's work and is dropped on reset. | The lead keeps using `delegate_task`; re-assigning a member continues its session by default and `fresh: true` asks for a clean one (an independent check). The continuation message carries the team conversation since that member's last assignment and the files others changed. A session is continued only after a run that ended with a final message; after an error it restarts. The current assignment is anchored so compaction keeps it verbatim. Sessions last for one task. `MA_RESUME_MEMBERS=0` restores the previous behaviour. |
| Checks on an assignment (`delegate_task.checks`, `check_lines`) | aider runs the test command after the model edits files and feeds failures back: [`Coder` auto-test](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py#L1616-L1623), [`cmd_test`/`cmd_run`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/commands.py#L993-L1011) (Apache-2.0); Inspect AI's separation of execution and scoring (see `evaluation.py` below) | Run a command (or file checks) after the work and put the failing output in front of the model that decides what happens next. | The checks belong to one assignment and run on a temporary checkout of the *published* project, so they see what the team sees. Results go to the lead (and, in a review loop, to the next reviewer) as information; they never stop delivery. Invalid checks (e.g. `..` paths) are refused before any work starts. |
| Parallel hand-offs (`Tool.concurrent`, `Agent._run_together`) | Codex runs tool calls that declare parallel support under a shared lock and all others under an exclusive one: [dispatch](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/parallel.rs#L223-L233), [lock choice](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/parallel.rs#L343-L356), test [`completed_and_budget_stopped_tools_report_their_own_lifecycle`](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/parallel.rs#L1069-L1100) (Apache-2.0) | Calls from one model response may run at once when the tool allows it; results are returned in call order. | The decision is per batch, from the arguments: several `delegate_task` calls run together only for distinct teammates that each declare `deliverables`, with no file or directory shared, no review assignments, and enough delegation budget. Anything else runs one after another, as before. `MA_PARALLEL_DELEGATIONS=0` turns it off. |
| Review loop (`review_loop`) | AutoGen's reflection pattern: a [`RoundRobinGroupChat`](https://github.com/microsoft/autogen/blob/98e6bba168c784cc3d8ae9491453241b10f4f5ee/python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/_round_robin_group_chat.py#L72-L82) alternates writer and critic until [`TextMentionTermination`](https://github.com/microsoft/autogen/blob/98e6bba168c784cc3d8ae9491453241b10f4f5ee/python/packages/autogen-agentchat/src/autogen_agentchat/conditions/_terminations.py#L111-L155) sees the stop text from a given source (MIT) | Author and reviewer alternate without the orchestrator relaying each turn; a fixed marker in the reviewer's message ends the loop, and a round cap bounds it. | The reviewer must start with `VERDICT: APPROVED` or `VERDICT: CHANGES REQUESTED`; findings (without the verdict line) go to the author's continued work session, the next review continues the reviewer's review session, and assignment checks run after each fix. Each round counts against the delegation budget; the lead gets one result with the verdict, per-round lines, the last review and the files changed. A missing verdict counts as changes requested. |
| Reviewer model and thinking (`Settings.reviewer_model`, `reviewer_thinking`) | Codex lets a spawned agent override the parent's [`model` and `reasoning_effort`](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L610-L647) (Apache-2.0) | A sub-agent can run on a different model or reasoning setting from the one that wrote the work. | Applied to review work only (`review: true` assignments and review-loop reviews); empty means the authors' settings, so the default is unchanged. Review work keeps a session separate from the same member's work session, because thinking and non-thinking histories cannot be mixed. Settable per task in 运行选项 or with `MA_REVIEWER_MODEL` / `MA_REVIEWER_THINKING`. |
| Visible plan (`update_plan`) | Codex's [`update_plan` tool](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/plan_spec.rs#L40-L50) and [handler](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/plan.rs#L66-L100) (Apache-2.0); Magentic-One's [progress ledger](https://github.com/microsoft/autogen/blob/98e6bba168c784cc3d8ae9491453241b10f4f5ee/python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/_magentic_one/_prompts.py#L59-L121) (MIT) | The coordinator keeps an explicit list of steps with statuses, replaced as a whole on each update, and the host shows it. | No extra ledger model call: the lead updates the plan as a tool call when it chooses. Several steps may be in progress at once (parallel work). Members see the current plan in their context; the chat shows each version and folds earlier ones. `MA_PLANS=0` turns it off. |
| Lessons after a task (`retrospective.py`) | CrewAI's [`TaskEvaluator.evaluate`](https://github.com/crewAIInc/crewAI/blob/56c26a7fb3ece62c64de52e8b4228d15c9c81492/lib/crewai/src/crewai/utilities/evaluators/task_evaluator.py#L69-L113) asks for suggestions after each task ([schema](https://github.com/crewAIInc/crewAI/blob/56c26a7fb3ece62c64de52e8b4228d15c9c81492/lib/crewai/src/crewai/utilities/evaluators/task_evaluator.py#L28-L35), [tests](https://github.com/crewAIInc/crewAI/blob/56c26a7fb3ece62c64de52e8b4228d15c9c81492/lib/crewai/tests/utilities/evaluators/test_task_evaluator.py#L11-L68)) (MIT) | After a task, one model call reviews what happened and writes actionable suggestions for future tasks. | Suggestions become `proposed` knowledge for a named member (scope: this group, or general for a library role) and are used only after the requester approves them with one click in the chat. At most four, specific and reusable; an unparsable answer or any error leaves the task untouched. Off in benchmarks; `MA_RETROSPECTIVE=0` turns it off. |

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

A room turn receives the selected folder's identity and a bounded, sorted root listing before its first model call. The read-only evidence tool can page directories and read relative text paths from that folder, including edits made outside Studio since the last task. Workspace reads never import or export files, acquire a worker checkout, or start work. Subsequent lookup turns expose only the read tool; historical deliveries still use the task ID and snapshot. Task workers receive the selected folder identity, the private checkout's relative-path convention and the imported project listing.

The initial directory listing follows Gemini CLI's [sorted, bounded folder traversal and unreadable-directory handling](https://github.com/google-gemini/gemini-cli/blob/eecccfcd5aff25de5e78135ca20f7abe3f566955/packages/core/src/utils/getFolderStructure.ts#L95-L172) and [truncation tests](https://github.com/google-gemini/gemini-cli/blob/eecccfcd5aff25de5e78135ca20f7abe3f566955/packages/core/src/utils/getFolderStructure.test.ts#L142-L180). Studio pages subsequent directories on demand and applies its checkout exclusions. See `tests/test_bound_workspace.py` for live folder reads and task checkout coverage.

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
a saved pause reserves its session until the user resumes or ends it. Running records left behind without a saved pause become interrupted. Other queues continue normally.
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

**Comparing collaboration set-ups** — `python -m miniagent bench --out <dir>` runs
the Mid_Test scenarios with the real model in three arms: `team` (continued member
sessions), `team-fresh` (every assignment starts a new conversation) and `solo`
(one agent holding all four role descriptions, 400 steps by default). Both tasks of a
scenario run in one group, in autonomous mode. Scenario 1 runs one arm at a time
because agent sandboxes share the host's network ports. A copy of each task's
project, chat and final answer goes to `<dir>/<arm>/<scenario>-task<n>/` for
blind review. `results.jsonl` and `summary.md` record status, minutes, steps,
hand-offs (and how many continued a session), prompt tokens, cache hits and
files. With `--min-balance` no task starts once the DeepSeek balance falls below
that amount. `tests/test_bench.py` drives all three arms offline.

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

The composer immediately echoes a submission in its room, captures attachments and clears the input. A client message ID reconciles that bubble with the room draft and the durable input. Sends from one tab are ordered per room, so the user can type and send a follow-up while a preceding request is pending. A failed submission retains its text and attachments for an explicit retry. Studio checks committed IDs under the turn lock. A repeated ID returns the original receipt without executing another model turn.

The assistant bubble shows a processing state when the router accepts the message, a thinking state on the first provider reasoning fragment, and public text as it arrives. Background tabs release the event stream and reconnect using the room snapshot when visible again. Browser regressions hold the message POST, thinking phase and successive HTTP body chunks independently, and exercise consecutive sends, failures and retries. The immediate user append and distinct pending and streaming states follow [Vercel AI SDK Chat](https://github.com/vercel/ai/blob/ai%406.0.0/packages/ai/src/ui/chat.ts) and its [chat tests](https://github.com/vercel/ai/blob/ai%406.0.0/packages/ai/src/ui/chat.test.ts).

## Verification

* `python -m unittest discover -s tests -t .` — offline suite (scripted model):
  loop termination and wrap-up, the re-reading regression, protocol validity,
  steering, cancellation, compaction, delegation/publishing/isolation,
  questions, budgets, @mentions, cross-task context, HTTP API and preview origin.
* `python -m miniagent demo` — offline end-to-end team run.
* Live runs of the Mid_Test scenarios: see [mid-test-status.md](mid-test-status.md).


Each Studio process snapshots its bundled UI assets at startup and advertises
`chat_protocol: 1` in bootstrap. Running an old API with JavaScript loaded
from a newer checkout previously left both the local echo and the saved user
message visible, while the missing stream endpoint returned 404. A page that
encounters a pre-stream backend now shows its capability and renders the
durable exchange once. Restarting the instance on the same port and data
directory enables streaming for its existing rooms.

On a streaming backend each committed public assistant event carries its
transient `draft_id` when available. The client promotes the same DOM bubble
to committed Markdown, including when polling beats the final stream frame.
A late snapshot cannot resurrect it. Outgoing user bubbles use their client
IDs the same way. The acceptance button uses an ID scoped to that room and
delivery so another click or a reload does not repeat the receipt. Ordinary
submissions with identical text still have separate IDs. The identity-based
update follows [Vercel AI SDK Chat.makeRequest](https://github.com/vercel/ai/blob/ai%406.0.0/packages/ai/src/ui/chat.ts#L574-L599).

## Groups and sessions

A collaboration group owns one roster and lead. The original group ID continues to address its default session. Its chat log, workspace binding, published project, task IDs and links stay at their original locations. Additional sessions use an address of the form `g-…-s-…` and live in `groups/<team>/sessions/<session>/`. Their manifest stores the display name, creation time, optional workspace binding and archive flag. Group settings are resolved from the owner on load and after a roster edit.

The browser requests `GET /api/groups/<team>/sessions` or the directory projection `GET /api/groups?view=conversations`. `POST /api/groups/<team>/sessions` accepts a name and returns the new room. Existing message, task, stream, preview, export and file endpoints take the returned room ID. The session owns its timeline, local notes, attachments, active worker, queue, working copies and published versions. Each new upload goes to `POST /api/groups/<room>/uploads` and carries the room ID. Explicit folder bindings for siblings must be disjoint. Approved general role knowledge can still follow the role between rooms.

An unaddressed work request goes to the current group's lead, who can do the work or use synchronous delegation to chosen members. A leading mention selects a primary member for that task. Ordinary follow-ups use the current session's read-only evidence. A subsequent independent task in the same session can reuse earlier deliveries. Super Agent can create sessions and dispatch to their addresses; direct work in an existing group needs no additional Super Agent turn.

The channel/topic split and composer interaction were informed by Zulip's [topic-scoped message lookup](https://github.com/zulip/zulip/blob/a57ea58f7b32ef1fe34ea1ee0e6c5d40e08879d2/zerver/lib/topic.py#L81-L91) and [mention insertion](https://github.com/zulip/zulip/blob/a57ea58f7b32ef1fe34ea1ee0e6c5d40e08879d2/web/src/composebox_typeahead.ts#L1459-L1491). Studio's task and filesystem boundaries are implemented locally. `tests/test_group_sessions.py` exercises this contract with disk reloads, concurrent workers and a browser submitting work to two sessions of the same team.

## Studio-wide assistant controls and DeepSeek protocol recovery

The Studio assistant can inspect a specific session, apply roster and settings changes, stop a named task and relay user input to active assignments, including work commissioned directly in another room. The chat router handles single-step controls. Multi-step requests open an assistant task with the directory, role and knowledge tools, session and workspace settings, batch dispatch, redispatch, and delivery inspection. Ordinary members keep their room-scoped tools. Changing a workspace uses the same checks as the settings panel. The local provider key is managed in Studio settings.

Each batch item specifies a destination and brief. Each accepted item produces an independently persisted dispatch receipt and outcome. A retry of the same assignment under the same assistant task reuses its task ID. Reassignment preserves the original brief, initial attachments and checks, with an optional new brief or primary member. Only those attachments are copied into the receiving session. The new task records its predecessor. Studio accepts the destination before cancelling the exact source task. A queued cancellation becomes terminal immediately. A running cancellation reports `stopping` until the worker exits. A repeat cannot stop the next occupant of the room. The per-recipient dispatch design draws on [LangGraph Send](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py#L689-L728).

DeepSeek's HTTP adapter distinguishes public text from native structured tool calls. It withholds possible DSML markers across streaming boundaries. When a response contains only textual tool markup, the adapter requests a fresh native decision once with the original conversation, tool permissions and remaining request deadline. It counts both requests. Textual markup supplies no operation arguments. A second malformed response becomes a visible protocol error. The original on-disk chat history is preserved. Historical protocol text is replaced with an explanatory note when read for chat. The router can recover an incomplete decision from the original room context without letting file excerpts authorize operations. Failed decisions record a diagnostic code on the room reply. This follows the [DeepSeek tool contract](https://api-docs.deepseek.com/guides/tool_calls/) and the distinction between dedicated markers and ordinary quoted text in [vLLM's V4 parser and streaming tests](https://github.com/vllm-project/vllm/blob/main/rust/src/parser/src/tool/deepseek_dsml/deepseek_v4.rs). Studio receives HTTP text and lacks the tokenizer metadata available to vLLM.

Offline incident replays and task-control checks live in `tests/test_supervisor_controls.py`. The slow HTTP browser test in `tests/test_chat_stream.py` compares the user bubble's right edge, the assistant bubble, and the composer before and after commitment at narrow and desktop widths.

## Pause and continue an execution

Click **⏸ 暂停** beside the chat composer or in task details. **已暂停** confirms that the private continuation file has been committed. **▶ 继续执行** returns to the same task ID, including after a Studio restart. **结束任务** makes cancellation final and releases queued work. During a graceful Studio shutdown the same pause request is sent to every worker. The main process waits for their tool boundaries. A process killed before a checkpoint is committed can leave an interrupted record. Old executions that never captured runtime state retain their existing history and deliveries.

The session has one active work slot. An acknowledged pause reserves the role working directories, project baselines and FIFO position. Other sessions can keep working. The private `tasks/<id>/continuation.json` is written by atomic replacement with owner-only permissions. It contains the original roster, execution time, steering cursors, pending question and assignment, plus one model frame for every active role. Frames hold messages including tool protocol state, usage, compaction state, loop guard and the next unresolved call in a batch. The HTTP task-detail and artifact views do not serve this file. On resume the worker claims the checkpoint before invoking tools. A crash during resumed work cannot replay an obsolete saved frame.

A running foreground tool finishes before pausing. A model request interrupted before returning a complete decision can be requested again. Previously confirmed tool results remain in history. Delegated members restore their existing working copies, human questions keep their IDs and an assistant waiting for a dispatched task rejoins its existing receipt. Pausing ends local background processes; handles returned by the shell tool preserve the final output and exit or cancellation status for collection by the resumed agent.

The management API exposes `POST /api/tasks/<id>/pause`, `resume` and `cancel`. The bootstrap flag `task_pause` gates the browser controls. The pause response can report `pausing` while an operation settles. The room exposes `paused_task_id` after commitment. Paused work produces no delivery notice or acceptance result.

The task ID and completed-call handling follow the recovery pattern in [LangGraph's checkpoint loop](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/pregel/_loop.py) and its [test that a successful sibling is skipped on resume](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/tests/test_pregel_async.py#L1748-L1785). Studio implements cooperative checkpoints in its existing file-backed runtime. `tests/test_pause_resume.py` covers browser controls, an actual CLI process restart, nested assignments, outstanding shell results, queue ordering and cross-room waits.

Pause also covers the collaboration features. Assignments in flight are kept per assignee (`inflight`), so several parallel hand-offs can be paused and each rejoins its own member frame. When a pause reaches part of a parallel batch, the hand-offs that already finished keep their results in the lead's frame and only the paused ones run again, as in LangGraph's skipped successful sibling. A review loop saves its round, phase, last review, findings and check results (`loop_state`) and continues the same round. Member sessions, their compaction state and the lead's plan are part of the checkpoint, so a continued member still remembers its earlier work after a restart. `tests/test_pause_collaboration.py` pauses inside both, restarts Studio and resumes.
