# Rebuild proposal: a model-driven Studio on the bounded core

Branch `jinyuwu/model-driven-studio` (worktree `../MultiAgentNLP-rebuild`), based on
`jinyuwu/bounded-core` (`eeeff2a`). The original Studio is kept for reference on
`jinyuwu/integrate-bounded-core` (`530df33`); read old code with
`git show jinyuwu/integrate-bounded-core:<path>`. Do not merge it back.

## Implementation and verification

The Studio entry point on this branch now runs the bounded core for the assistant,
private chats and teams. Current interfaces, persistence and checks are described
in [architecture.md](architecture.md). The [acceptance record](mid-test-status.md)
distinguishes offline replay from real model and artifact results. The phases
below preserve the original proposal.

## 1. Why

The original runtime let **host code decide the workflow**: phase policies swapped
what the lead could see each step (only the last 4 turns), a review ledger decided
whether work counted (signatures, frozen hashes, "a different script cannot clear a
failure"), regexes gated questions, validators blocked publishing, and budgets were
unlimited. Evidence:

* `runs/studio-20261121-043842-6fc84dad0251`: 4,436 lead calls, 8,586 `run_shell` in 2.7 h (re-reading loop).
* `runs/studio-20261122-021637-1b3915630460`: working app, QA 31/31 after fixing its own script, still `[BLOCKED]` by the ledger.

The bounded core lets the **model decide** and the host only keep it safe. On the
same Mid_Test Task 1 runs it finished 3/3 (newsroom 57 min / 5 delegations,
fund 61 min / 4, software 89 min / 6; 22–29 M tokens each, mostly cached prompt).

## 2. Invariants (do not violate; review every PR against these)

1. **The model decides the workflow.** Who works next, what is reviewed, when to ask
   the user, when work is done. The host never picks the next actor, never blocks an
   answer or a handoff on its own verdict, never rewrites the model's context by phase.
2. **The host only enforces**: sandbox isolation; finite budgets and termination
   (steps, delegations, repeat refusal, loop guard, tool-less wrap-up report);
   protocol validity (every tool call answered); cancellation; persistence;
   redaction of the configured API key; permissions the user grants explicitly
   (e.g. writing to a local folder).
3. **No silent forgetting.** Full history each step; compaction (`context.py`) only.
4. **Everything bounded.** No "0 = unlimited". UI options are bounded numbers.
5. **Evaluation is not control.** Acceptance checks score a finished task and are
   shown to people; they may be *told* to the lead as information, never gate it.
6. **Every action observable**: tool calls/results, delegations, reports, questions,
   publishes, compactions, loop events go to `trace.jsonl` and the UI.

Forbidden patterns (these created the old failures): review/signature ledgers,
frozen-candidate hashes as gates, host-chosen "bound handoffs", regex classifiers on
model text that refuse actions, per-phase compact views, intake/read allowances,
credential heuristics beyond the configured key, file-type allow-lists, new hooks
into `Agent.run` (extend through tools instead), "unlimited" sentinels. If a live run
fails, fix prompts, tools or information — do not add a gate.

## 3. What exists on this branch (keep, extend)

| Module | Role |
| --- | --- |
| `agent.py` | bounded loop, tool-less wrap-up, steering inbox, process cleanup |
| `context.py`, `loop_guard.py` | Pi-style compaction; OpenHands-style stuck detection + re-read rule |
| `team.py` | `TeamTask`: lead + `delegate_task` / `ask_human` / `remember`, prompts, cross-task context |
| `workspace.py` | private agent workspaces ⇄ team `project/` (checkout / publish / log) |
| `store.py` | groups, `chat.jsonl`, tasks, traces, memory, journal under `runs/groups/` |
| `studio.py`, `server.py`, `web/` | service, HTTP API (+ preview origin), simple UI |
| tools (`tools.py`, `file_tools`, `shell_tool`, `browser_tool`, `research`, …) | unchanged action space |
| `cli.py`, `metrics.py` | studio / team / run / demo / report / smoke |

Tests: `python -m unittest discover -s tests -t .` → 175 OK. Design notes and
upstream links: `docs/architecture.md`.

## 4. Feature disposition (original → rebuild)

| # | Original feature (reference files) | Decision | Rebuild design |
| --- | --- | --- | --- |
| 1 | Groups, DMs, @mention (`studio_conversations`, `studio_channels`) | rewrite | DM = group with one member. Add rename, pin, archive, delete-to-trash, member edit. `group.json.kind = group|dm|assistant`. |
| 2 | Pinned groups + chat directory (`studio_channels`, `directory.js`) | rewrite | `pinned`, `archived`, `last_activity` on groups; directory view with search/filter. |
| 3 | Role library: saved roles reused across groups (`studio_roles`, `runs/studio-profiles.json`) | rewrite | `runs/roles/<id>/role.json` (id, name, prompt, timestamps). Groups reference role ids; member prompt = role prompt (+ optional group note). Editing a role affects future tasks only. |
| 4 | Role knowledge: proposed/approved notes & skills, revisions, restore, scopes (`studio_role_memory`, `role_tools`) | rewrite | `runs/roles/<id>/knowledge.jsonl` (append-only revisions: id, text, scope general|group:<gid>, status proposed|approved|rejected|archived). Agent tools: `propose_knowledge(text, scope)` (replaces `remember`), `recall_knowledge(query)`. Only **approved** entries are put in context; the user approves in the UI. Group `memory.md` notes become group-scoped knowledge. |
| 5 | Superagent / unified assistant (`studio_superagent`, `studio_dispatch`, `studio_route_results`) | rewrite | A normal `Agent` in a `kind=assistant` conversation with tools: `list_roles`, `list_groups`, `create_role`, `create_group`, `start_task(group_id, text)` (async, returns task id), `task_status(task_id)` (status + final answer + changed files), `read_group_file(group_id, path)`. Model decides routing; the host never rewrites the user's request. |
| 6 | Message attachments / uploads (`studio_uploads`, `attachment_tools`) | rewrite | Immutable originals `runs/uploads/<sha256>/` (+ meta). Message events carry attachment refs. Tools: `list_attachments`, `read_attachment(offset)`, `copy_attachment_to_workspace(path)`. Only the configured key is redacted. |
| 7 | Local folder binding + publish back (`studio_workspace`, `/api/workspaces/pick`) | rewrite | `group.json.folder = {path, imported_manifest}`. Before a task: import folder → project (new or changed files). After the task (and after each publish if enabled): export changed project files; if the folder file changed since import, do **not** overwrite — write `<name>.miniagent-conflict` and report. Folder picker via macOS `osascript` as before. |
| 8 | Steering while running (`studio_steering`) | exists | keep `HumanChannel`; add "send as next task" (queue, FIFO, one per group) as a UI option. |
| 9 | Live reply streaming (`studio_reply_stream`, SSE) | partial | core emits `delta` events; UI polls. Optional: SSE endpoint `/api/groups/<id>/stream`. |
| 10 | Questions to the user (`human_handler`) | exists | keep; add "let the team decide" button that answers with a standard sentence. |
| 11 | Per-message thinking toggle, interaction mode, budgets (`_request`) | rewrite | task options `{thinking, interaction_mode, member_max_steps, lead_max_steps, max_delegations}` validated against bounded ranges; passed into `TeamTask` (per-task `Settings` copy). |
| 12 | Task list + task detail tabs: chat / overview / logs / artifacts (`app.js`, `tool_activity`) | rewrite | `/api/tasks` across groups; detail = timeline slice, activity log from `trace.jsonl`, changed files from `project-log.jsonl`, metrics. |
| 13 | Previews: HTML sandbox, markdown reader, images (`studio_preview`, `markdown-reader.js`) | keep/port | HTML on preview origin (exists); port `markdown-reader.js/.css`. |
| 14 | Downloads: project zip, task artifacts zip, room bundle for assessors (`studio_discussion`) | rewrite | `/archive.zip` exists; add task-artifacts zip and **assessor export** (chat.jsonl rendered to Markdown + all files) — Mid_Test asks to hand dialogue + outputs to an evaluating model. |
| 15 | User verdict on a delivery (`review_delivery`) | rewrite as message | UI buttons "accept" / "request changes" post a normal user message; no host state. |
| 16 | Acceptance files/checks (`studio_acceptance`, `acceptance.py`) | rewrite as evaluation | `evaluation.py`: after a task, run declared checks (file exists, contains, command exit 0) in a copy of the project; store a score in `task.json`; show in UI; optionally include the list in the lead's task text. Never blocks (invariant 5). |
| 17 | Diagnostics (`studio_diagnostics`) | rewrite small | summarize from trace: provider errors, budget hits, loop-guard events, cancelled. |
| 18 | Status questions without a run (`studio_group_status`, `studio_room_reply`) | restore room-scoped turns | The addressed role answers follow-ups from room evidence. Explicit changes steer the same active task. Independent work starts or queues. |
| 19 | Recover failed member / inherited reviews / signed reviews / probes / peer recheck / coordinator workspace / check provenance / tool_progress / intake allowance / read cap | **drop** | replaced by invariants 1–2; published partial work already persists. |
| 20 | Credential heuristics, file-type allow-lists, snapshot validators (`project_data`) | **drop** | keep configured-key redaction and 25 MB file cap. |
| 21 | CLI `eval` (batch metrics), `orchestrate` DAG, `tasks` (SQLite TaskStore), `report` | `eval` rewrite, drop `orchestrate`/`tasks` | `eval --tasks file.jsonl` runs single-agent or team tasks with evaluation checks → completion rate, mean/p95 duration, tokens (course metrics). TaskStore/orchestrator are superseded by the lead-driven team + JSON store. |
| 22 | Web API key entry, own-instance launcher, jade theme (commit `530df33`) | port | see Phase 1. |
| 23 | Mid_Test templates | exists | keep `examples/teams/*.json`. |

## 5. Target layout and API

Keep the flat package (no mass move). New modules: `roles.py` (role + knowledge
store), `uploads.py`, `folders.py`, `superagent.py`, `evaluation.py`, `export.py`,
`settings_api.py` (key verify + `.env` writer). `studio.py` stays the service facade.

Storage under `runs/`: `groups/<gid>/…` (exists), `roles/<id>/role.json|knowledge.jsonl`,
`uploads/<sha>/original|meta.json`. Writes are atomic (temp + `os.replace`).

HTTP (same origin, mutations require `Origin` = page origin **and** an
`X-Studio-Token` from `/api/bootstrap`, as in the original `studio_server._guard`;
replace the current `X-Studio: 1` header):

```
GET  /api/bootstrap                      config, token, templates, key hint
POST /api/settings/api-key               {api_key}
GET  /api/roles  POST /api/roles  PATCH /api/roles/<id>
GET  /api/roles/<id>/knowledge           POST …/knowledge/<entry>/status {approved|rejected|archived|restore}
GET  /api/groups?filter=  POST /api/groups  PATCH /api/groups/<id>  (rename, pin, archive, members, folder)
GET  /api/groups/<id>?after=&activity_after=
POST /api/groups/<id>/messages {text, attachments[], options{}, queue:bool}
POST /api/groups/<id>/cancel
GET  /api/groups/<id>/files | /files/<path> | /archive.zip | /export.zip
GET  /api/tasks?group=  GET /api/tasks/<tid> (record, activity, changes, evaluation)
POST /api/uploads (multipart or base64 JSON, ≤ 20 MB)
POST /api/folders/pick
GET  /api/assistant (assistant conversation = a group with kind=assistant)
```

## 6. Phases (each ends green: all tests pass + the listed manual check)

**Phase 0 — setup.** `ln -s ../MultiAgentNLP/.venv .venv` (or create a venv and
`pip install -r requirements.txt && python -m playwright install chromium`); copy
`.env`. Run tests and `python -m miniagent demo`.

**Phase 1 — port settings, launcher, theme** (from `530df33`: `studio_service.set_api_key`,
`_verify_deepseek_key`, `_write_env_value`, `studio_server.serve`, `web/theme.css`,
`tests/test_studio_settings.py`). New server guard with token. Launcher: busy port →
next free port, never reuse another process. Tests: key verified/rejected/unreachable,
`.env` 0600, token required, busy-port move.

**Phase 2 — UI shell port.** Bring the original web app (`git checkout
jinyuwu/integrate-bounded-core -- miniagent/web/` into `web_legacy/` for reference),
rebuild `web/` with the original structure and jade theme: sidebar (assistant,
chats, roles, pinned groups, tasks, connection card), room view (timeline, member
panel, files panel, question banner, steering composer), settings dialog, new-group
dialog with templates. Data layer talks only to §5 API. Playwright UI smoke test
(`tests/test_ui_smoke.py`) against a server with `FakeProvider`.

**Phase 3 — roles & knowledge** (feature 3–4). Import existing
`runs/studio-profiles.json` and approved entries of `runs/studio-role-memory.sqlite3`
read-only into the new store (`python -m miniagent migrate`, idempotent). Tools
`propose_knowledge`, `recall_knowledge`; approval UI.

**Phase 4 — conversations** (features 1, 2, 8, 10, 11): DM, pin/archive/rename,
member edit, per-task options, queue, "let the team decide".

**Phase 5 — attachments** (feature 6) and **local folders** (feature 7) with
conflict files; tests for import/export/conflict/no-overwrite.

**Phase 6 — Superagent** (feature 5). Tests with `FakeProvider`: creates a role,
a group, starts a task, polls status, reports the final answer.

**Phase 7 — task views, previews, exports, evaluation, diagnostics, CLI eval**
(features 12–17, 21). Metrics: completion rate, mean/p95 duration, tokens,
delegations, questions.

**Phase 8 — core tuning from live runs**: members often used 74–80 of 80 steps and
the fund lead used 60/60 → defaults member 100, lead 80, and an optional bounded
`max_steps` argument on `delegate_task` (≤ 150). Tool output messages are Chinese and
pull answers into Chinese → make tool messages English (or match the task language).
Consider `compact_at_tokens` 80 k for members to cut token use. General web search
engines block bots; keep `search_sources` + `http_get`, optionally add a configurable
search API key.

**Phase 9 — cleanup & docs**: update `README.md`, `docs/architecture.md`, remove
`web_legacy/`; AOCI index is rebuilt separately via the AOCI Guide (`aoci_rules`).

## 7. Acceptance for the whole rebuild

1. **Platform regression**: full offline suite green; must include termination tests
   (`test_agent_loop.py`, re-read regression), team protocol, HTTP guard, UI smoke.
2. **Real-model runs**: all 6 Mid_Test tasks (3 scenarios × Task 1 then Task 2 in the
   same group), interactive, a human answering questions. Each ends `completed`,
   none > 120 min, no `loop_stuck` on a lead, Task 2 visibly reuses Task 1 files.
3. **Original-task acceptance**: independent check of the delivered artifacts
   (run the booking app and dashboard; verify article/fact-check sources; recompute
   the proposal's weights/amounts; open the HTML slides page by page). Report 1–3
   separately, per `AGENTS.md`, with run ids, durations, tokens.
4. **Feature checklist** in the UI: every row of §4 marked rewrite/port works by hand.

Each PR description: which invariant it touches, upstream references read (pinned
links, see `docs/architecture.md`), tests added, and the real failure it covers.
