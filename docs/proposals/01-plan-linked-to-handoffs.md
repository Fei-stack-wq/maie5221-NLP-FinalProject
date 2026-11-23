# Proposal 01 — Plan steps linked to hand-offs

Status: proposed · 2026-11-22 · base: `jinyuwu/model-driven-studio` at `3a9284c`
Depends on: nothing. Recommended first of the three proposals.

## Problem

The lead's plan (`update_plan`, `TeamTask._update_plan`) is a snapshot the lead writes. Hand-offs
(`delegate_task`, `review_loop`) are not linked to plan steps, so Studio knows what happened but not
which step it belongs to, and progress only changes when the lead spends a model call on
`update_plan`. The chat counts completed steps in the last snapshot (`app.js`, `case "plan"`).

Every finished task in `runs/` that kept a plan (checked 2026-11-22):

| Task | Plan progress over the task | Hand-offs | Hand-offs tied to a step |
| --- | --- | ---: | ---: |
| Newsroom v2 `t-339d2ce6eac3` | 1/7 → 7/7 | 7 | 0 |
| Newsroom v2 `t-d45634663202` | 1/7 → 7/7 (1/7 while 9 hand-offs had finished) | 9 | 0 |
| Fund `t-4b0396433521` | 1/7, unchanged after delivery | 4 | 0 |
| Fund `t-50ffaaca63d0` | 0/6 → 11/11 | 8 | 0 |
| Software `t-9156d5ba3e7a` | 1/6 → 5/6 → 6/6 | 5 | 0 |
| Software `t-9c2ae7fc38e5` | 0/6 for ~50 min → 6/6 | 6 | 0 |

The fund deck in `t-4b0396433521` had 12 slides where the plan said 13: ticking the old plan at
delivery would also have been wrong. The user saw "0 progress" for most of each run.

## Goals

- The plan shows what actually happened (assigned, submitted, reviewed) as it happens, with no
  extra model calls.
- Whether submitted work is acceptable stays the lead's judgement.
- No step shows "to do" when work for it has been submitted.

Non-goals: Studio dispatching work from the plan by itself; Studio judging quality.

## Design

**Platform records, lead judges.** Each step carries two independent things:

1. *Evidence*, derived by Studio from events: `assigned` (who, when), `submitted` (report status,
   published files with digests, check results), `reviewed` (verdict from a review assignment or
   review-loop round).
2. *Acceptance*, set only by the lead through `update_plan`: `pending`, `in_progress`, `completed`.
   Add `rework` ("needs changes") as a fourth value.

**Stable step IDs.** `update_plan` steps get an optional `id` (short slug, e.g. `draft`). Studio
assigns `s1…sn` when missing, and on a later update keeps the ID of a step whose `id` is passed back
or whose text is unchanged; anything else is a new step. The tool reply lists the IDs
(`Plan updated. Steps: s1 Research …, s2 Draft …`) so the lead can reference them.

**Hand-offs name their step.** `delegate_task` and `review_loop` take an optional `step` (ID). An
unknown ID is accepted but recorded as unlinked, and the tool result says so with the valid IDs —
no refusal, so no wasted round trip. The delegation and report events carry `step`; review-loop
rounds carry it on every round.

**One derived view.** A new function `plan_view(record, events) -> dict` (new module
`miniagent/plan_view.py`) folds the current plan with the task's `delegation`/`report` events:

```json
{"steps": [{"id": "s2", "step": "Write first draft", "owner": "reporter", "status": "in_progress",
            "after": [1],
            "evidence": {"assigned": {"agent": "reporter", "seq": 41, "time": 1795401234.5},
                         "submitted": {"status": "completed", "seq": 58, "files": [{"path": "04_article_draft.md", "sha256": "…"}],
                                       "checks": {"passed": 3, "total": 3}},
                         "reviewed": {"verdict": "changes requested", "seq": 66}}}],
 "unlinked": [{"seq": 72, "assignee": "editor", "task": "…"}],
 "counts": {"total": 7, "in_progress": 1, "submitted": 3, "accepted": 2}}
```

Multiple hand-offs on one step: the latest of each kind wins; the full list stays in `history`.
After the task ends, a step that was submitted but never accepted reads "submitted, not confirmed".

**Prompts.** Lead: "When you delegate work for a plan step, pass its `step` ID. Studio records
assignment, delivery and review on the plan by itself; call `update_plan` only to change the plan or
to accept or send back a step." Members see the plan with evidence (`render_plan` adds
"submitted by reporter: 04_article_draft.md").

**Fast mode.** `fast_profile` sets `plans=False` today because each update cost a model call. With
progress automatic, enable plans in fast mode with "write the plan once; you do not need to update it".

## Changes by file

| File | Change |
| --- | --- |
| `miniagent/team.py` | `_plan_tool`: step `id`, status `rework`. `_update_plan`: ID assignment and matching, reply lists IDs. `_delegate_tool`/`_review_tool` schemas: `step`. `_delegate_one` → `_assign` → `_open_assignment`: pass `step`; add it to the `delegation` event. `_close_assignment`/report append: add `step`. `_review_loop`: pass `step` to every round. `render_plan`: IDs and evidence. `lead_prompt`: the sentence above. `continuation_state`/restore: nothing new (IDs live in `self.plan`). |
| `miniagent/plan_view.py` (new) | `plan_view(record, events)` as above; pure function, unit-testable. |
| `miniagent/studio.py` | `state()["live"]` adds `plan` = `plan_view(...)` for the running task; task summary (`GET /api/tasks/<id>?view=summary`) adds `plan_view`. |
| `miniagent/config.py` | `fast_profile`: `plans=True`. |
| `miniagent/web/app.js` | One live plan card per task rendered from `live.plan` / the task summary; older `plan` events render as a one-line "计划已更新" divider without counts. Counter "进行中 a · 已提交 b · 已确认 c / n". Evidence line per step with file links (reuse `artifactCards`/`dispatchLink` link format `…/tasks/<id>/files/<path>?sha256=`). "其他派工" section for unlinked hand-offs. |
| `miniagent/web/workbench.js` | Task details: show the same plan view on the 概览 tab. |
| `miniagent/web/app.css` | Styles for evidence lines and states (submitted = cobalt, accepted = celadon, rework = gold, unconfirmed = muted). |
| `miniagent/bench.py` | Row adds `plan_link_rate` (linked hand-offs / hand-offs) and `update_plan_calls`. |
| `docs/architecture.md` | Update the "Visible plan" row and add upstream links. |

Switch: `MA_PLAN_LINKS` (default on). Off = current behaviour.

## Tests (offline, through `Studio.post`)

1. Hand-off with `step` → `plan_view` shows `assigned` immediately and `submitted` with files after
   the report, with no `update_plan` call in between; counts update.
2. Unknown `step` → hand-off runs, appears under `unlinked`, tool result lists valid IDs.
3. IDs survive a plan rewrite that reorders steps and keeps one step's text; a renamed step without
   `id` becomes a new step.
4. Review loop with `step` → every round linked; verdict shown on the step.
5. Task ends with a submitted, unaccepted step → "submitted, not confirmed", never "to do".
6. Pause/resume (`tests/test_pause_resume.py` style) keeps IDs and evidence.
7. UI (Playwright, like `tests/test_ui_smoke.py`): live card updates on report; older plan events
   show no counts.
8. Fast mode: plan written once, evidence still updates.

## Measurement and acceptance

- `bench` on the three Task 1s: ≥ 80 % of hand-offs linked to a step; `update_plan` calls per task
  not higher than today (the 6 tasks above averaged 2.0); no step "to do" with submitted evidence.
- Blind quality check unchanged or better (the change touches bookkeeping, not the work).

## Risks

- The lead forgets `step`: unlinked hand-offs stay visible under "其他派工", and the prompt plus the
  tool reply (listing IDs) make linking the easy path.
- Evidence mistaken for acceptance: the UI keeps "submitted" and "accepted" visibly distinct.

## Upstream (read before implementing; cite pinned links in the commit and `docs/architecture.md`)

- Codex `update_plan` tool spec and handler:
  [plan_spec.rs L40-L50](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/plan_spec.rs#L40-L50),
  [plan.rs L66-L100](https://github.com/openai/codex/blob/44848822b1a58fa40cdcd89ec3161c757735cfd0/codex-rs/core/src/tools/handlers/plan.rs#L66-L100)
  (Apache-2.0) — whole-plan replacement with statuses.
- Magentic-One progress ledger:
  [_prompts.py L59-L121](https://github.com/microsoft/autogen/blob/98e6bba168c784cc3d8ae9491453241b10f4f5ee/python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/_magentic_one/_prompts.py#L59-L121)
  (MIT) — the orchestrator tracks progress per turn; here the evidence half comes from events instead
  of a model call.
