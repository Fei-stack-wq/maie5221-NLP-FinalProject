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
| Continued member sessions (`AgentSession`, `TeamLimits.resume_members`) | Codex CLI multi-agent tools: [`send_input` spec](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L166-L194) and the `fork_context` default [L625-L631](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L625-L631), [`AgentControl::send_input`](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/agent/control.rs#L138-L168), tests [`send_input_errors_when_thread_missing`](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/agent/control_tests.rs#L909-L922) and [`send_input_submits_user_message`](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/agent/control_tests.rs#L985-L1007) (Apache-2.0); AutoGen [`AssistantAgent` keeps its model context between calls](https://github.com/microsoft/autogen/blob/98e6bba168c784cc3d8ae9491453241b10f4f5ee/python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py#L108-L114) and clears it only in [`on_reset`](https://github.com/microsoft/autogen/blob/98e6bba168c784cc3d8ae9491453241b10f4f5ee/python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py#L1622-L1624) (MIT) | A follow-up to an existing agent is appended to that agent's own history instead of starting a new one; a new agent starts with only its prompt unless told otherwise; the conversation lives for the duration of the parent's work and is dropped on reset. | The lead keeps using `delegate_task`; re-assigning a member continues its session by default and `fresh: true` asks for a clean one (an independent check). The continuation message carries the team conversation since that member's last assignment and the files others changed. A session is continued whenever every tool call in it has its result, including after a run that ended in an error; a history cut inside a tool batch restarts. The current assignment is anchored so compaction keeps it verbatim. Sessions last for one task. `MA_RESUME_MEMBERS=0` restores the previous behaviour. |
| A member keeps its progress after a failed model request (`TeamTask._run_member`, `AgentSession.resumable`) | Codex retries a failed sampling request inside the same turn, keeping the turn's history: [retry budget](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/session/turn.rs#L2131), [`handle_response_stream_error` in the sampling loop](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/session/turn.rs#L2270-L2297), test [`retries_on_early_close`](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/tests/suite/stream_no_completed.rs#L26-L60); an agent no longer loaded is [rebuilt from its recorded rollout](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/agent/control/resume.rs#L101-L123) (Apache-2.0) | A transient model failure does not discard the work done so far; the same conversation continues. | The HTTP client already retries a request; when the member's run still ends with `model request failed`, Studio waits 5 seconds and continues the member's own session once with a note that its conversation and workspace are intact, with the steps it has left (at least 10). If the turn ended on a user message, a short assistant note keeps the turns alternating. If it fails again, the lead gets the member's last progress note instead of an empty report, and the next assignment to that member continues the same session. Metrics record `recovered_after_error`. |
| Studio assistant as one agent with a stable tool set (`assistant.py`) | LangGraph supervisor: [ordinary tools and hand-off tools bound into one tool node](https://github.com/langchain-ai/langgraph-supervisor-py/blob/88859b34017ac3569bbd4a3092c7e77593a0a960/langgraph_supervisor/supervisor.py#L164-L201), [`test_supervisor_basic_workflow`](https://github.com/langchain-ai/langgraph-supervisor-py/blob/88859b34017ac3569bbd4a3092c7e77593a0a960/tests/test_supervisor.py#L165) (MIT) | One ReAct agent holds both its ordinary tools and its hand-offs and decides per step whether to answer, look something up or delegate. | A hand-off is a `dispatch_task` receipt: the target team runs on its own and posts its outcome back into the chat, instead of returning control to the supervisor within the same run. |
| Confirmation before deletion (`delete_*`, `decide_approval`) | OpenAI Agents SDK human-in-the-loop: [`needs_approval` and the approval flow](https://github.com/openai/openai-agents-python/blob/6c4e4a29cd08eef454db6980d74da6afc8939d24/docs/human_in_the_loop.md#L11-L72), [`evaluate_needs_approval_setting`](https://github.com/openai/openai-agents-python/blob/6c4e4a29cd08eef454db6980d74da6afc8939d24/src/agents/util/_approvals.py#L37-L57), [`test_memory_session_hitl_scenario`](https://github.com/openai/openai-agents-python/blob/6c4e4a29cd08eef454db6980d74da6afc8939d24/tests/test_hitl_session_scenario.py#L89-L143) (MIT) | Sensitive tool calls are not executed; they surface for a person to approve or reject, and an approved call runs with its recorded arguments. | The run is not paused and resumed. The turn ends with one card; approval executes the frozen calls after checking that their scope digest is unchanged, and the next turn sees the outcome in the chat. |
| Checks on an assignment (`delegate_task.checks`, `check_lines`) | aider runs the test command after the model edits files and feeds failures back: [`Coder` auto-test](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py#L1616-L1623), [`cmd_test`/`cmd_run`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/commands.py#L993-L1011) (Apache-2.0); Inspect AI's separation of execution and scoring (see `evaluation.py` below) | Run a command (or file checks) after the work and put the failing output in front of the model that decides what happens next. | The checks belong to one assignment and run on a temporary checkout of the *published* project, so they see what the team sees. Results go to the lead (and, in a review loop, to the next reviewer) as information; they never stop delivery. Invalid checks (e.g. `..` paths) are refused before any work starts. |
| Parallel hand-offs (`Tool.concurrent`, `Agent._run_together`) | Codex runs tool calls that declare parallel support under a shared lock and all others under an exclusive one: [dispatch](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/parallel.rs#L223-L233), [lock choice](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/parallel.rs#L343-L356), test [`completed_and_budget_stopped_tools_report_their_own_lifecycle`](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/parallel.rs#L1069-L1100) (Apache-2.0) | Calls from one model response may run at once when the tool allows it; results are returned in call order. | The decision is per batch, from the arguments: several `delegate_task` calls in one response run together unless one says `independent: false` (since 23 Nov the default; requiring `independent: true` on each was easily forgotten), for distinct teammates that each declare `deliverables`, with no file or directory shared, no brief that names another call's files or teammate, valid arguments and enough delegation budget. A check (review) of finished work may join a batch; the same rules refuse one that reads, names or reviews what another call produces, and it reports without the follow-up below, while its batch-mates see its findings in theirs. Background hand-offs in the same response start alongside the batch but are not joined in its follow-up. Anything else runs one after another, as before. `MA_PARALLEL_DELEGATIONS=0` turns it off. |
| Follow-up check after parallel hand-offs (`ParallelBatch`, `TeamTask._reconcile`) | LangGraph's deferred node waits for every branch of a fan-out before it runs: [`defer`](https://github.com/langchain-ai/langgraph/blob/1.0.2/libs/langgraph/langgraph/graph/state.py#L369), [the deferred join barrier](https://github.com/langchain-ai/langgraph/blob/1.0.2/libs/langgraph/langgraph/graph/state.py#L1045-L1066), test [`test_in_one_fan_out_state_graph_defer_node`](https://github.com/langchain-ai/langgraph/blob/1.0.2/libs/langgraph/tests/test_pregel.py#L2029-L2091) (MIT) | Work that ran in parallel is joined, and the join sees every branch's actual output. | The lead can be wrong that two assignments are independent. So when a parallel batch finishes, each teammate gets one more turn in its own session (30 steps) with the others' reports and published files, and either updates its files or replies "No change needed". The lead receives one report per assignment, with the follow-up appended; follow-ups are not extra delegations. Every way out of an assignment (report, refusal, pause, error, cancel) releases the others' wait. A pause during the follow-up finishes it on resume; if a pause or failure stopped the join, the lead's result says the check did not run and suggests a follow-up. |
| Review loop (`review_loop`) | AutoGen's reflection pattern: a [`RoundRobinGroupChat`](https://github.com/microsoft/autogen/blob/98e6bba168c784cc3d8ae9491453241b10f4f5ee/python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/_round_robin_group_chat.py#L72-L82) alternates writer and critic until [`TextMentionTermination`](https://github.com/microsoft/autogen/blob/98e6bba168c784cc3d8ae9491453241b10f4f5ee/python/packages/autogen-agentchat/src/autogen_agentchat/conditions/_terminations.py#L111-L155) sees the stop text from a given source (MIT) | Author and reviewer alternate without the orchestrator relaying each turn; a fixed marker in the reviewer's message ends the loop, and a round cap bounds it. | The reviewer must start with `VERDICT: APPROVED` or `VERDICT: CHANGES REQUESTED`; findings (without the verdict line) go to the author's continued work session, the next review continues the reviewer's review session, and assignment checks run after each fix. Each round counts against the delegation budget; the lead gets one result with the verdict, per-round lines, the last review and the files changed. A missing verdict counts as changes requested. |
| Reviewer model and thinking (`Settings.reviewer_model`, `reviewer_thinking`) | Codex lets a spawned agent override the parent's [`model` and `reasoning_effort`](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L610-L647) (Apache-2.0) | A sub-agent can run on a different model or reasoning setting from the one that wrote the work. | Applied to review work only (`review: true` assignments and review-loop reviews); empty means the authors' settings, so the default is unchanged. Review work keeps a session separate from the same member's work session, because thinking and non-thinking histories cannot be mixed. Settable per task in 运行选项 or with `MA_REVIEWER_MODEL` / `MA_REVIEWER_THINKING`. |
| Plan evidence and acceptance | Codex [plan schema](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/plan_spec.rs#L40-L50) and [handler](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/plan.rs#L66-L100), Apache-2.0. Magentic-One [progress ledger](https://github.com/microsoft/autogen/blob/98e6bba168c784cc3d8ae9491453241b10f4f5ee/python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/_magentic_one/_prompts.py#L59-L121), MIT. | The lead replaces an ordered plan and judges acceptance. | Stable step IDs join actual delegation and report events to the live and historical view. Multiple handoffs, reviewed files and checks remain auditable. A submission can remain unconfirmed after delivery. |
| Addressed mail and interjections | Codex [message, interrupt and follow-up schemas](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L193-L306) and [control tests](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/agent/control_tests.rs#L985-L1007), Apache-2.0. | A recipient can receive a note at a turn boundary or be interrupted. | The task persists recipients, read receipts and interruption state. A synchronous handoff can temporarily consult the lead on a separate, balanced history. Its decision is visible in the room and in the lead's next turn. |
| Check-ins and peer answers | CrewAI [coworker question](https://github.com/crewAIInc/crewAI/blob/56c26a7fb3ece62c64de52e8b4228d15c9c81492/lib/crewai/src/crewai/tools/agent_tools/ask_question_tool.py#L8-L28), [target lookup and errors](https://github.com/crewAIInc/crewAI/blob/56c26a7fb3ece62c64de52e8b4228d15c9c81492/lib/crewai/src/crewai/tools/agent_tools/base_agent_tools.py#L46-L124), MIT. Codex [fork context](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L625-L631), Apache-2.0. | A member can address a factual question to a named coworker. | A bounded read-only copy of the coworker's session answers. The coworker gets a record, and the asker's assignment continues. A check-in uses the lead's consult instead. |
| Communication measurements | OpenTelemetry [GenAI agent metrics](https://github.com/open-telemetry/semantic-conventions-genai/blob/4181b1ebe55da7edb3538636c45ae64af369b5ef/docs/gen-ai/gen-ai-metrics.md#L595-L886), Apache-2.0. | Record model operation time and usage per actor. | Studio distinguishes auxiliary calls from task steps, records delivery latency per recipient, and counts actual interruptions, questions and notices. |
| Lessons after a task (`retrospective.py`) | CrewAI's [`TaskEvaluator.evaluate`](https://github.com/crewAIInc/crewAI/blob/56c26a7fb3ece62c64de52e8b4228d15c9c81492/lib/crewai/src/crewai/utilities/evaluators/task_evaluator.py#L69-L113) asks for suggestions after each task ([schema](https://github.com/crewAIInc/crewAI/blob/56c26a7fb3ece62c64de52e8b4228d15c9c81492/lib/crewai/src/crewai/utilities/evaluators/task_evaluator.py#L28-L35), [tests](https://github.com/crewAIInc/crewAI/blob/56c26a7fb3ece62c64de52e8b4228d15c9c81492/lib/crewai/tests/utilities/evaluators/test_task_evaluator.py#L11-L68)) (MIT) | After a task, one model call reviews what happened and writes actionable suggestions for future tasks. | Suggestions become `proposed` knowledge for a named member (scope: this group, or general for a library role) and are used only after the requester approves them with one click in the chat. At most four, specific and reusable; an unparsable answer or any error leaves the task untouched. Off in benchmarks; `MA_RETROSPECTIVE=0` turns it off. |
| Task meter (`meter.py`, `model_call` events, live bar, 耗时 tab, soft budgets) | OpenTelemetry GenAI metrics: [`gen_ai.client.operation.duration`](https://github.com/open-telemetry/semantic-conventions-genai/blob/4181b1ebe55da7edb3538636c45ae64af369b5ef/docs/gen-ai/gen-ai-metrics.md#L54-L66), agent [`invoke_agent.duration`, `inference_calls`, `tool_calls`](https://github.com/open-telemetry/semantic-conventions-genai/blob/4181b1ebe55da7edb3538636c45ae64af369b5ef/docs/gen-ai/gen-ai-metrics.md#L595-L886) (Apache-2.0); OpenAI Agents SDK [`RequestUsage`](https://github.com/openai/openai-agents-python/blob/6c4e4a29cd08eef454db6980d74da6afc8939d24/src/agents/usage.py#L151-L194) and [`Usage.add`](https://github.com/openai/openai-agents-python/blob/6c4e4a29cd08eef454db6980d74da6afc8939d24/src/agents/usage.py#L257-L315), which keep per-request input, cached and output tokens (MIT) | Measure each model operation's duration and token usage, and count inference and tool calls per agent. | Nothing is exported: each model call emits a `model_call` trace event (seconds, model, prompt, cached, output and reasoning tokens) and each tool result carries its seconds. `TaskMeter` aggregates them per agent for the live bar, the task's `metrics.time`, an estimated cost from DeepSeek's price table (cache hits, peak and off-peak hours) and alerts (a tool over 60 s, 3 minutes without progress, a run at 80 % of its steps). Optional soft budgets (minutes, tokens) send the lead a notice at 50 %, 80 % and 100 %; nothing is stopped. |
| Reads run together (`Tool.read_only`, `READ_ONLY_TOOLS`) | Codex marks read and web-search tools as [supporting parallel calls](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/ext/web-search/src/tool.rs#L95-L97) ([memories read](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/ext/memories/src/tools/read.rs#L49-L51)) and runs them under a shared lock (see parallel hand-offs above) | Read-only tool calls from one response run at once. | A batch made only of reads, searches and fetches runs on up to six threads; anything that writes runs one after another as before. Fetched sources are saved under content-hash names, so parallel fetches cannot write the same file differently. The prompts ask agents to batch independent calls, because the Mid_Test runs averaged 1.1 tool calls per model response. |
| Warm browser and short browser timeouts (`BrowserSession`, `playwright_runner.py --serve`) | Playwright MCP keeps one browser per session, reuses it across tool calls and closes it when idle: [`createBrowserWithInfo`/idle timer](https://github.com/microsoft/playwright/blob/049226728d2da8054db18b04782a9848b755855e/packages/playwright-core/src/tools/mcp/browserFactory.ts#L54-L66), [isolated launch](https://github.com/microsoft/playwright/blob/049226728d2da8054db18b04782a9848b755855e/packages/playwright-core/src/tools/mcp/browserFactory.ts#L123-L137) (Apache-2.0) | One browser for many checks. | One Chromium per agent run, under the same sandbox wrapper, environment, timeout and cancellation as a shell command, closed when the run ends; every call still gets a fresh browser context. Actions and `expect` fail after 5 s and navigation after 15 s (Playwright defaults to 30 s): in the Mid_Test runs 50 failing browser checks waited about 21 minutes in total. A cold start measured 0.6–1.2 s against 0.1 s warm, so the timeouts matter more than the warm browser. `MA_WARM_BROWSER=0` uses a new browser per call. |
| Dependency-aware planning (`delegate_task.inputs`, plan steps with `after`) | CrewAI validates that an asynchronous task does not take another concurrent asynchronous task as context and that no task depends on a later one: [`validate_async_task_cannot_include_sequential_async_tasks_in_context` and `validate_context_no_future_tasks`](https://github.com/crewAIInc/crewAI/blob/56c26a7fb3ece62c64de52e8b4228d15c9c81492/lib/crewai/src/crewai/crew.py#L857-L899) (MIT) | Work that runs at the same time must not consume each other's output; dependencies point backwards. | A hand-off names the project files it reads (`inputs`); calls run together only if none reads another's deliverables, on top of the existing `independent` flag, disjoint files and follow-up check. Inputs reach the member as "read these first", and inputs missing from the project are reported to the lead. Plan steps may name earlier steps in `after`, by step ID or number (anything else is refused with the valid IDs and numbers); the tool reply lists the steps that are ready together, and the chat shows what a step waits for. When a hand-off, review loop or background hand-off reports, Studio reads the recorded evidence and, if two or more pending steps with different owners now have all their dependencies done, adds one line naming them and how to start them together. Studio still never starts a step itself. This followed the S1T1 run of 23 Nov, where QA and the release were both unblocked by the build but ran one after the other. A whole-plan update that omits `after` for a step keeps its earlier dependencies (by step ID, so they follow reordering); `after: []` clears them. When hand-offs the lead marked `independent: true` cannot run together, each result names the reason (for example, two of them for the same teammate, a file both would write, or one reading what another produces) instead of falling back silently. These followed the v3.3 runs of 23 Nov. The lead's own edits are published before a hand-off's inputs are checked, so a file it just wrote is not reported missing. When a hand-off follows a step the lead owns that the plan does not show as done, its result says so, with the step's declared outputs that are missing and the files the lead published since the plan was written; nothing is refused, since the plan may be what is wrong. `update_plan` repeats each step's declared outputs. These followed the S2T1-v3.4 run. The lead's prompt names safe pairs (release notes, guides or test plans drafted while the build or tests run; separate research topics) and unsafe ones (a risk review before the research, an outline before the reporting, client material before the analysis), after the fund run of 22 Nov started a risk review in parallel with its research. |
| Fast mode (`fast_profile`, run option `mode: fast`, composer toggle ⚡ 快速) | aider's architect mode: the main model plans and hands each answer to a separate editor model that makes the edits ([`ArchitectCoder`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/architect_coder.py#L6-L48), [`editor_model` settings](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/models.py#L145-L146)) (Apache-2.0) | The model that decides reasons; the model that executes is the cheaper, faster one. | DeepSeek needs the same thinking setting across one conversation, so the split is per agent: the lead and reviewers keep thinking at low effort, members' own work runs without thinking, all on `deepseek-flash`. The task has a hard 15-minute limit (every run gets the time that is left and then writes its report), a soft 10-minute budget that warns the lead, 30 member and 25 lead steps, four hand-offs, at most two review rounds, two-minute tool timeouts, and no plan updates or lessons. Prompts ask for the simplest working result and for what was left out. A `thinking: disabled` choice also turns the lead's thinking off, and an explicit `reasoning_effort` replaces fast mode's low effort. The quality cost has not been measured yet; `bench --arms fast` is the comparison. |
| Hand-off kinds (`delegate_task.kind`, `config.KIND_PROFILES`, `kind_settings`) | Codex lets a spawned agent take a role (`agent_type`) and override the parent's [`model` and `reasoning_effort`](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L610-L647), with the override guidance at [L17-L24](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L17-L24) (Apache-2.0) | The work being handed off decides how much the sub-agent reasons and which model runs it. | The lead tags the shape of the work, not the domain: `research`, `make`, `check`, `fix`, `adapt`. Only `check` keeps the requester's effort; research/make/fix think at low effort; `adapt` runs without thinking, without `run_browser` and with one-minute commands; `check` caps one browser script at 60 s. Default step budgets 60/100/50/40/25, never above the task's. A kind only lowers cost: it never turns thinking on or raises the effort, and when the task's options set `thinking` (the composer's 思考 choice or the assistant's dispatch option) every kind keeps that thinking mode and effort and only the budget and tools change. A task option `reasoning_effort` (`low`, `high`, `max`; the composer's 强度 choice, or the assistant's dispatch option when the user asks for an effort) sets the effort for the lead and every hand-off that thinks; `adapt` still works without thinking unless `thinking` is set too. Without it, the effort comes from `DEEPSEEK_REASONING_EFFORT` (default `high`). `review: true` is a `check`; review-loop rounds are `check` and `fix`. Untagged hand-offs keep the previous settings. The system prompt is the same for every kind (the kind note is in the assignment), and `adapt` keeps its own session, so a continued session keeps one thinking mode, one tool list and its prompt cache. `MA_HANDOFF_KINDS=0` turns it off. |
| Preloaded inputs and `read_files` (`handoffs.preload`, `tools._read_files`) | aider puts read-only files into the chat instead of letting the model fetch them: [`get_read_only_files_content`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py#L659-L670), [`get_readonly_files_messages`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py#L763-L775) (Apache-2.0) | Files the model needs as reference are in its context from the start. | The hand-off's declared `inputs` (files, or the text files of a folder) are read from the member's fresh checkout and appended to the assignment, up to 24,000 characters (12,000 per file, 30 files); the rest is listed for `read_files`. `read_files` reads up to 20 files in one call (80,000 characters). After three single read-only calls in a row an agent is reminded, at most twice per run, to batch reads (`MA_BATCH_NUDGE`). `MA_PRELOAD_INPUTS=0` turns preloading off. |
| Background hand-offs (`background: true`, `wait_handoffs`) | Codex spawns agents that run while the parent continues and collects them with [`wait_agent`](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L303-L311) (Apache-2.0); CrewAI's [`async_execution`](https://github.com/crewAIInc/crewAI/blob/56c26a7fb3ece62c64de52e8b4228d15c9c81492/lib/crewai/src/crewai/task.py#L184-L191) starts a task and [joins pending asynchronous tasks](https://github.com/crewAIInc/crewAI/blob/56c26a7fb3ece62c64de52e8b4228d15c9c81492/lib/crewai/src/crewai/crew.py#L2044-L2057) before the next synchronous one (MIT) | The coordinator can keep working while a sub-agent runs, and collects its result later. | `delegate_task` with `background: true` (the default for `adapt`) opens the assignment, starts the member in its own thread and returns at once. The report is recorded as usual and reaches the lead as a note at its next step, or as the result of `wait_handoffs`. Instead of CrewAI's automatic join, a hand-off to the busy teammate, or one that writes or reads files a background hand-off produces, is refused with a pointer to `wait_handoffs`; a lead that answers while hand-offs still run waits for them and gets one short turn (8 steps) to update its answer. The lead's workspace takes in published files at its next step boundary, after publishing its own edits. A background member's check-in goes to the lead's inbox instead of a consult while the lead is working. Pause joins every background member (each saves its frame); resume starts them again. In a response with other hand-offs, a background one starts at the same time as them, wherever it appears, when the batch rules allow it; it is not joined with them and reports through the lead's inbox (since S1T1-v3.5, where an `adapt` sent after QA waited for QA). `MA_BACKGROUND_HANDOFFS=0` turns it off. |
| Evidence for check, fix and adapt (`handoffs.evidence_block`) | CrewAI gives a task the raw outputs of the tasks in its `context`: [`Crew._get_context`](https://github.com/crewAIInc/crewAI/blob/56c26a7fb3ece62c64de52e8b4228d15c9c81492/lib/crewai/src/crewai/crew.py#L1905-L1913) (MIT) | Later work starts from what earlier work produced, without being told by hand. | Studio keeps the last 30 finished hand-offs (report, published files, Studio check results, browser findings, verdict). A `check`, `fix` or `adapt` hand-off receives up to 9,000 characters of them, those touching its inputs or deliverables first; a member continuing its own session does not get its own reports again, and a review-loop fix does not (its brief has the findings). `adapt` is told to rely on them and not to re-test. When a hand-off publishes files that a background hand-off reads or produces, that member gets a note. `MA_HANDOFF_EVIDENCE=0` turns it off. |

### Team communication and rollout

Hand-off kinds, preloaded inputs, the batching reminder, background hand-offs and
evidence passing are on by default and each has a switch (`MA_HANDOFF_KINDS`,
`MA_PRELOAD_INPUTS`, `MA_BATCH_NUDGE`, `MA_BACKGROUND_HANDOFFS`,
`MA_HANDOFF_EVIDENCE`). They are covered by offline tests through `Studio.post`
(`tests/test_handoff_kinds.py`); their effect on real-model time, cost and
deliverable quality has not been measured yet. Task metrics record
`handoff_kinds` and `background_handoffs` for that comparison.

With `MA_PLAN_LINKS=1`, the chat and task tree project two independent counts from the same task events.
A finished assignment counts as completed work when its declared checks passed and its deliverables published.
Confirmation records an explicit step acceptance by the lead or an unqualified approval from its assigned reviewer.
A request for changes reopens the step. A new work assignment or changed lead output invalidates a previous signature.
A running review can leave already completed work awaiting confirmation.

A handoff may name several stable IDs in `step_ids`. In a review loop, `retest_steps` adds coverage only on
reviews after a fix. The same report sequence and files appear under each covered step. Lead-owned steps can
bind exact project-relative `outputs` or declare that the final answer delivers the step with `on_delivery`.
For a decision or other non-file result, `record_plan_result` records a short result for specific step IDs. Its optional `decision`
field lets the lead accept or return those steps with a reason.
These events update progress without another whole-plan call. An unrelated final answer leaves other steps open.

Historical evidence can be attached with `POST /api/tasks/<id>/plan-evidence` and a `links` array of
`{source_seq, step_ids}` objects. References are task-local, batched atomically and idempotent.
The projection uses the original evidence timestamp and reviewer, even if the reference was added later.
`MA_PLAN_LINKS=0` retains snapshot plans.

Studio enables addressed mail, urgent steering, lead consultations and teammate
tools by default, including for work dispatched by the Super Agent. Members decide
when to ask or send a note. Explicit `0` values in `MA_MAILBOXES`, `MA_INTERRUPTS`,
`MA_LEAD_CONSULT` or `MA_TEAM_TALK` in the Studio environment override the defaults.
Peer tools depend on mailboxes and check-ins also require lead consultations.
The local `GET /api/config` response reports the loaded switches under `team_communication`.
In a running task, address a requirement to a member with a leading
mention. The composer shows the intended recipients. Select **立即打断** for urgent
steering. Each recipient's receipt reflects actual delivery. An ordinary follow-up
still goes through conversation routing. A task checkpoint retains unread mail and
the progress of open assignments. The team bench explicitly enables the whole profile.

Members may send notes, ask a peer for evidence, or ask the lead for a decision.
A long silent assignment receives at most one check-in reminder. Defaults per
assignment are three check-ins, three peer questions and five notes. Fast mode
uses one, one and two respectively. A lead may consult up to ten times per task
(three in fast mode). Requests at the limit return a visible outcome. The meter
includes the tokens reported by consults and peer answers. A provider can omit
usage for an interrupted stream, so the dollar estimate can undercount it.

The bench exports plan-link rate, plan calls, similar repeat briefs, talk counts
and quota hits, and normal/urgent delivery p95. Requester delivery latency uses one sample
per actual read receipt for user mail. Browser minutes are grouped by the roles explicitly assigned
reviews within this task. Tasks without a named verifier have unattributed minutes.
This measure helps locate repeated checks; it does not establish that two checks
covered the same behavior.

For a controlled steering experiment, pass `--interjection "…"` to `bench`.
Each still-running benchmark task receives the same requirement after 300 seconds
(override with `--interject-after`). The benchmark binds delivery to the original
task. A task that finishes before the injection is recorded as such. Verify the
requested change in the saved deliverable. Compare the three Task 1 deliverables
blind, along with wall time, cost, token usage and human interventions.

### Browser checks

Every context created through the supplied `browser` inherits a 5 s action timeout and a
15 s navigation timeout, including the implicit context owned by `browser.new_page()`.
A page or operation can override these defaults. Studio closes all the call's contexts on exit.
Playwright implements the standalone page with an owned context. [Playwright Python browser source (v1.48.0)](https://github.com/microsoft/playwright-python/blob/v1.48.0/playwright/_impl/_browser.py)

For automatic refresh or polling, pass `virtual_time: true` to `run_browser`.
Studio installs Playwright Clock before the script navigates. After loading the page
and changing the underlying data, call `page.clock.fast_forward(30_000)` for a 30 s
interval and assert that the updated data appears. Each independent context has its
own clock. Ordinary checks use real time by default, and network requests and the
application server still run in real time. The browser regressions exercise periodic
fetches in fresh windows through both warm and cold runners.
[Playwright Clock source (v1.48.0)](https://github.com/microsoft/playwright-python/blob/v1.48.0/playwright/_impl/_clock.py) · [Playwright Clock guide](https://playwright.dev/python/docs/clock)

An unsuccessful `run_browser` call records a structured `browser_report` on its
`tool_result` trace event. A Playwright operation timeout identifies the waiting
statement and preserves the driver's call log. A failed assertion records the
script location, the exception text and any values available from that evaluation.
Playwright supplies its own expected/actual diagnostic. Python `assert` records
bounded representations of evaluated operands for ordinary scalars and collections.
Custom objects are never inspected to reconstruct a value.
[Playwright assertion implementation (v1.48.0)](https://github.com/microsoft/playwright-python/blob/v1.48.0/playwright/_impl/_assertions.py)

Small markers inserted via Python's AST record entered statements even if the
runner is interrupted. At the whole-script deadline the location is the **last
entered statement**. When an exception escapes, its traceback identifies the
failing statement and callers. Later statements in those blocks that were never
entered appear with line numbers. This is statement coverage for the submitted
script. It cannot enumerate runtime-generated checks or untested loop iterations.
The model receives a compact list, and the trace and expandable chat card retain
the full list. Successful checks keep their existing output.
[Python abstract syntax trees](https://docs.python.org/3/library/ast.html#ast.NodeTransformer)

For example, if a booking check expects `Booked` but sees `Pending`, the report
records that value and lists the following cancellation check as unreached. The
member's handoff carries the unsuccessful calls in execution order, with their
positions among all browser calls. The lead can use these observations to decide
what to do next. Recording evidence triggers no extra navigation, assertion,
model call or assignment. The regression in `tests/test_browser_evidence.py`
follows a real Chromium result through the tool trace, lead handoff and chat UI.

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

**Where task time goes** — Measured from the trace timestamps of the nine Mid_Test Task 1 runs
(2026-11-22): waiting on the model was 65–88 % of each run (330–470 model calls per team task, median
about 4 s, slowest tenth 11–20 s), agents made 1.1 tool calls per response, and browser checks took
15–21 minutes of each team's software task, mostly failing steps waiting 30 s. The speed changes
target that waste and keep every check and review: batching and parallel reads (fewer model round
trips), 5 s browser timeouts and a warm browser, and prompts that give verification an owner (authors
run fast checks, the reviewer or tester does the end-to-end walk-through, the lead relies on their
evidence and on Studio's check results). Each change must be measured with `bench` and a blind quality
review before it is accepted.

**Metrics** — `task.json` records status, duration, steps, tool calls,
delegations, questions and provider-reported tokens for the lead and each
assignment; `python -m miniagent report` aggregates completion rate, mean/p95
duration and tokens.

## Studio assistant (super agent)

The assistant (`assistant.py`) handles each chat message in one bounded agent loop: the same `Agent` the teams use, at most 40 steps. Its tools are the same in every turn:

- look up: `read_workspace`, `inspect_studio` (overview, chat, artifacts, tasks, files, search, trash), `inspect_room`, `task_status`, `read_attachment`, `list_roles`, `role_knowledge`;
- organize: `create_role`, `update_role`, `create_group` (group or DM, interactive or autonomous), `change_group_members`, `update_group_settings`, `create_session`, `set_studio_models`, `propose_role_knowledge`, `review_role_knowledge`;
- run work: `dispatch_task` (with forwarded attachments and task options), `message_task` (answer a task's open question or change its requirements), `pause_task`, `resume_task`, `cancel_task`, `redispatch_task`, `trash_conversation`, `restore_from_trash`;
- remove, only after confirmation: `delete_studio_data`, `delete_groups`, `delete_role`.

It does not write deliverables; hands-on work goes to a group or a DM through `dispatch_task`, and Studio posts the receipt and later the outcome into the chat (see below). Its system message holds the fixed instructions first, so the provider can reuse their cached prefix, followed by reference data: the current rooms, roles and models, its recent dispatches read from the targets' own records (so they survive a restart), the attachments in this chat, cards waiting for the user and the recent conversation. The user turn holds only the user's words.

The user's message is recorded before the turn's first change, so receipts and the reply follow it; a turn that fails before changing anything records nothing, and the browser keeps the text and attachments for its inline retry. A turn that changes something is saved as an `assistant_turn` record with its trace. That record is the origin of the turn's dispatch receipts and is not listed as a task; a conversational turn leaves only chat events. The reply lists the actions the turn took.

Deletion follows the approval flow of the OpenAI Agents SDK. A delete tool does not run. It validates its items at once — unknown IDs and busy conversations are refused to the model; a role still used elsewhere is held until the end of the turn, when it stays only if the conversations using it are on the same card — and adds them to one card per turn. The card stores the frozen calls and a digest of their scope. 确认执行, or `POST /api/groups/<assistant>/approvals/<id>` with `{"decision": "approve"|"reject"}`, resolves the items again and runs them only if the digest is unchanged; otherwise the card expires. A typed confirmation on a later turn goes through `answer_confirmation`, offered only while a card from an earlier turn waits and only for a message without attachments. Studio records, attachments, files and other conversations are data for the model; acting on them can at most produce a card.

The previous assistant ran a router model call that had to quote the user's exact words to unlock each action, switched to read-only after reading evidence, and moved commissioned work into a separate assistant task with a different tool set. Its abilities therefore changed between turns, and a request such as "read the attached role prompts, create the roles and the group, and dispatch Task 1" stopped after the read. That router remains for groups and DMs only.

A live DeepSeek check on 2026-11-22 replayed that request with the Mid_Test role and task files attached: one turn created the four roles with prompts identical to `01_role_initialization.md`, an autonomous group led by the Technical Lead, and a dispatch whose brief contains the Task 1 text; a deletion request produced only a card that listed the group, its roles and an empty artifact step, and approving it removed exactly those items. `tests/test_assistant.py` covers the same paths offline, plus confirmation, expiry, typed confirmation, attachment and cross-room safety, follow-up messages to dispatched tasks, cancellation and restarts.

## Studio workbench

The workbench opens existing rooms through the normal composer. Opening task details, traces or publications only performs reads. The overview follows the [integrated Studio workbench, tool disclosure and Markdown reader](https://github.com/Fei-stack-wq/maie5221-NLP-FinalProject/tree/530df33/miniagent/web). Its data comes from the bounded runtime.

- `GET /api/tasks` supplies the workbench and filters. `GET /api/tasks/<id>?view=summary` omits the large trace. The root identifies the task's primary agent. Each recorded `delegation` adds one branch, including repeat assignments to the same member. The subsequent `report` from that assignee supplies its outcome. A terminal task without a matching report shows that the report is missing.
- `GET /api/tasks/<id>/tools?cursor=<byte offset>` returns recorded calls and results, the next cursor and a more-pages flag. The chat disclosure matches results by actor and call ID in trace order. The model runtime determines which tools are called. The UI displays the stored argument and output previews.
- Report and publication cards link to `GET /api/tasks/<id>/files/<path>?sha256=<published digest>`. Task snapshots supply historical bytes. For older records without a snapshot the current file can supply the bytes if its digest matches. Missing revisions return 404. The room file panel opens the current project version.
- The reader uses the bundled markdown-it with raw HTML disabled. Document links and raster images resolve inside the originating task or project. Escape closes the reader and Back returns to the preceding document.

## Dispatch receipts in the assistant chat

When the assistant creates a room, the room record saves the originating group and the assistant turn. When it starts a task, the queued record saves the same provenance before the worker can run. The assistant chat receives `assistant_group_created` and `dispatch_sent` events at those commits. The receipt links to the actual room and task, including work queued behind another run.

A dispatch from the assistant does not wait for the child. The child runs independently and its outcome arrives as a notice; the assistant reads it, with the rest of its recent dispatches, in the next turn's reference data or through `task_status`. A queued task started from a script or another room with `dispatched_by` set to an assistant turn reports back the same way.

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

The assistant can inspect any conversation, change rosters and settings, stop, pause, resume or reassign a task, and pass the user's answer or new requirements to a running task, including work commissioned directly in another room. Several `dispatch_task` calls can go out in one step; each accepted dispatch has its own persisted receipt and outcome, and repeating the same dispatch within a turn reuses its task ID. Reassignment keeps the original brief, initial attachments and checks, with an optional new brief or primary member; only those attachments are copied into the receiving session. A target that is archived or busy is refused with the reason, leaving the original task in place.

DeepSeek's HTTP adapter distinguishes public text from native structured tool calls. It withholds possible DSML markers across streaming boundaries. When a response contains only textual tool markup, the adapter requests a fresh native decision once with the original conversation, tool permissions and remaining request deadline. It counts both requests. Textual markup supplies no operation arguments. A second malformed response becomes a visible protocol error. The original on-disk chat history is preserved. Historical protocol text is replaced with an explanatory note when read for chat. The router can recover an incomplete decision from the original room context without letting file excerpts authorize operations. Failed decisions record a diagnostic code on the room reply. This follows the [DeepSeek tool contract](https://api-docs.deepseek.com/guides/tool_calls/) and the distinction between dedicated markers and ordinary quoted text in [vLLM's V4 parser and streaming tests](https://github.com/vllm-project/vllm/blob/main/rust/src/parser/src/tool/deepseek_dsml/deepseek_v4.rs). Studio receives HTTP text and lacks the tokenizer metadata available to vLLM.

Offline incident replays and task-control checks live in `tests/test_supervisor_controls.py`. The slow HTTP browser test in `tests/test_chat_stream.py` compares the user bubble's right edge, the assistant bubble, and the composer before and after commitment at narrow and desktop widths.

## Pause and continue an execution

Click **⏸ 暂停** beside the chat composer or in task details. **已暂停** confirms that the private continuation file has been committed. **▶ 继续执行** returns to the same task ID, including after a Studio restart. **结束任务** makes cancellation final and releases queued work. During a graceful Studio shutdown the same pause request is sent to every worker. The main process waits for their tool boundaries. A process killed before a checkpoint is committed can leave an interrupted record. Old executions that never captured runtime state retain their existing history and deliveries.

The session has one active work slot. An acknowledged pause reserves the role working directories, project baselines and FIFO position. Other sessions can keep working. The private `tasks/<id>/continuation.json` is written by atomic replacement with owner-only permissions. It contains the original roster, execution time, steering cursors, pending question and assignment, plus one model frame for every active role. Frames hold messages including tool protocol state, usage, compaction state, loop guard and the next unresolved call in a batch. The HTTP task-detail and artifact views do not serve this file. On resume the worker claims the checkpoint before invoking tools. A crash during resumed work cannot replay an obsolete saved frame.

A running foreground tool finishes before pausing. A model request interrupted before returning a complete decision can be requested again. Previously confirmed tool results remain in history. Delegated members restore their existing working copies, human questions keep their IDs and an assistant waiting for a dispatched task rejoins its existing receipt. Pausing ends local background processes; handles returned by the shell tool preserve the final output and exit or cancellation status for collection by the resumed agent.

The management API exposes `POST /api/tasks/<id>/pause`, `resume` and `cancel`. The bootstrap flag `task_pause` gates the browser controls. The pause response can report `pausing` while an operation settles. The room exposes `paused_task_id` after commitment. Paused work produces no delivery notice or acceptance result.

The task ID and completed-call handling follow the recovery pattern in [LangGraph's checkpoint loop](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/pregel/_loop.py) and its [test that a successful sibling is skipped on resume](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/tests/test_pregel_async.py#L1748-L1785). Studio implements cooperative checkpoints in its existing file-backed runtime. `tests/test_pause_resume.py` covers browser controls, an actual CLI process restart, nested assignments, outstanding shell results, queue ordering and cross-room waits.

A restart can find a running task with no committed frame. Group and DM composers offer **从已保存进度继续** for the latest such interruption. A room message can request the same operation through the room router. The task detail also exposes the action for a selected interruption. `POST /api/tasks/<id>/recover` creates one successor with `continued_from`, the original brief and checks, the current project, the saved plan, outstanding assignments and the tail of the previous trace. Concurrent clicks and later retries return the existing successor ID. The original execution retains its interrupted status. The worker receives the last tool calls and confirmed results so it can check uncertain effects. Source attachments remain available in the same conversation.

Before the next checkout, Studio saves the outstanding members’ and primary role’s unpublished file differences under the interrupted task. Each role can retrieve its own files into a new workspace path. Historical interruptions followed by other work on older Studio versions may already have lost unpublished files. For mixed routing calls, evidence is read first. If an action follows, the router verifies it on the original conversation without the newly read file contents. With an interruption in scope a new commission must distinguish a separate brief from a continuation.

The distinction between recorded work and pending work follows [LangGraph 1.0.2’s persisted writes](https://github.com/langchain-ai/langgraph/blob/1.0.2/libs/langgraph/langgraph/pregel/_loop.py#L295-L346) and its [failed-node recovery test](https://github.com/langchain-ai/langgraph/blob/1.0.2/libs/langgraph/tests/test_pregel_async.py#L1766-L1821). Studio uses a new task identity when the execution frame is missing. Replay, duplicate clicks, room isolation, draft retrieval and browser playback are exercised in `tests/test_interrupted_recovery.py`.

Pause also covers the collaboration features. Assignments in flight are kept per assignee (`inflight`), so several parallel hand-offs can be paused and each rejoins its own member frame. When a pause reaches part of a parallel batch, the hand-offs that already finished keep their results in the lead's frame and only the paused ones run again, as in LangGraph's skipped successful sibling. A review loop saves its round, phase, last review, findings and check results (`loop_state`) and continues the same round. Member sessions, their compaction state and the lead's plan are part of the checkpoint, so a continued member still remembers its earlier work after a restart. `tests/test_pause_collaboration.py` pauses inside both, restarts Studio and resumes.

### Local Studio updates

The launcher compares SHA-256 digests of the Python package and bundled web assets at each launch. The process owning the data lock accepts an update request addressed to its verified instance ID. It drains running workers and HTTP writes before closing admission under the same lock used for task dispatch. The interpreter then starts fresh on the same port. This follows Werkzeug's fresh-interpreter reload approach, with the trigger tied to opening Studio and the handover tied to application activity. [Werkzeug 3.1.3 reloader](https://github.com/pallets/werkzeug/blob/3.1.3/src/werkzeug/_reloader.py#L245-L260)

### Studio assistant administration

The conversational router exposes the Studio operations from the same registry used by the assistant worker. Read-only lookups run in chat. The router keeps its immediate controls for tasks, membership and cleanup. Creating or editing roles, creating teams or sessions, and dispatching are also callable from chat. A validated first batch opens a durable assistant task and enters its ordinary tool loop. Queuing retains the batch, and the worker handles execution, tool errors, checkpoints and linked receipts. The group creation tool accepts the interaction mode at creation. A read-only inspector pages through directory, chat, work, publications and trash, and can read a specific current file or task revision without opening a task. A direct data mutation uses the current message's authorization and an exact conversation ID; the normal coordinator also uses the same service for multi-step commissions. Reading text from another room does not grant that text authority over the conversational router. Ordinary members are never offered these global tools.

Chat clearing atomically replaces the transcript with a higher sequence watermark, clears client-message caches, preserves notification deduplication and migrates attachment references needed by recorded assignments. Tabs with an earlier cursor reload their timeline. Permanent conversation deletion distinguishes the original team/default-session address from independent session roots. It removes scope-owned knowledge and generated dispatch receipts while retaining reusable roles, general knowledge and the user's external folders.

Artifact cleanup takes a session-owned publication path and rewrites the affected ZIP snapshots and publication history. A durable intent records digests before deletion so interrupted cleanup can finish on startup. A bound file is eligible only when its bytes match the synced publication in that binding. User edits, shared copies and symlinks are preserved. New publication records include their checkout scope to distinguish identically named files across folder changes; legacy records use recorded publication and sync digests. The receipts report preserved copies. Scoped file addressing and distinct trash/permanent deletion follow the approach in [Jupyter Server 2.14.2 FileContentsManager](https://github.com/jupyter-server/jupyter_server/blob/v2.14.2/jupyter_server/services/contents/filemanager.py) and its [contents manager tests](https://github.com/jupyter-server/jupyter_server/blob/v2.14.2/tests/services/contents/test_manager.py). Studio coordinates deletion with session workers and versioned artifacts.

A Studio assistant chat turn can carry several cleanup calls. Each one must cite the current user message. A directory lookup may precede them in the same turn. The write-capable continuation receives known identities, counts and typed status fields. After reading conversation text, workspace files or attachments, the assistant can continue to inspect or propose an operation. Studio resolves any proposed change independently from the original user message and room context. Arguments from the retrieved text do not pass through that boundary. For an authorized commission the worker reads the original room attachments itself. The originating chat event records lookup types and referenced IDs for diagnosis. When inspection and cleanup arrive in one provider response, the router returns the directory first and defers the proposed writes until the assistant has seen it.

The cleanup service resolves all targets under the Studio lock, deduplicates operations and checks busy sessions and role use before applying changes. It removes the selected published copies before deleting their conversations and deletes exclusive roles last. Other teams and restorable teams retain their roles. The assistant can also issue one call with team IDs and explicit choices for role and published-copy cleanup. A context-dependent choice without a confirmed target list produces a visible list in the assistant chat. Its structured scope survives restart. Confirmation recomputes the scope, so a newly added session or changed roster produces a revised list. An execution error stops dependent steps. The chat records individual results and the remaining work. Completed steps remain applied if a later filesystem operation fails.

The multi-call representation draws on [LangGraph 1.0.2 ToolNode](https://github.com/langchain-ai/langgraph/blob/1.0.2/libs/prebuilt/langgraph/prebuilt/tool_node.py#L633-L661). Studio serializes operations with deletion dependencies. The three-team replay and failure cases run in `tests/test_studio_admin.py`.
