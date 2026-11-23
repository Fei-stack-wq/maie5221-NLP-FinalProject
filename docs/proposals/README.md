# Proposals — communication and progress in a team task

Three proposals from the Mid_Test review of 22 Nov 2026. Each file is self-contained: problem with
evidence from `runs/`, design, changes by file, tests, measurement, risks and pinned upstream sources.

| # | Proposal | Fixes | Depends on | Size |
| --- | --- | --- | --- | --- |
| 01 | [Plan steps linked to hand-offs](01-plan-linked-to-handoffs.md) | Plan progress that sits at 0–1/7 while the team works; no link between plan and hand-offs | — | small |
| 02 | [Agent mailboxes, interjections and lead decisions during hand-offs](02-mailboxes-and-interjections.md) | Broadcast-only user messages, no interrupt, lead blind while a member works | — | medium |
| 03 | [Member check-ins, teammate questions and silence prompts](03-check-ins-and-teammate-questions.md) | 11–19 min silent runs, no way to ask a teammate, questions turning into re-delegations | 02 | medium |

**Order:** 01 first (independent, low risk, and it lets 02/03 attach messages to plan steps), then 02,
then 03. 01 and 02 can be built in parallel by different people: 01 touches the plan tool, the
delegation/report events and the plan card; 02 touches `HumanChannel`, `Agent.run` interruption,
the router's `STEER` action and the composer. Both edit `team.py`, so merge one before the other is
finished, or agree on who owns which functions.

## Implementation status

All three proposals are implemented on the collaboration branch. Offline tests use
the normal Studio message endpoint, checkpoints and a real local browser. Real
DeepSeek comparison and blind assessment of the original Task 1 deliverables
remain pending authorization. The runtime switches and measurement definitions
are documented in [the architecture](../architecture.md#team-communication-and-rollout).

In the offline comparison, the three original team templates each ran Task 1
through both the new and classic arms with scripted model responses and real
workspace tools. Six runs produced twelve files. All six incorporated an injected
requirement into a file and produced an independently checked SHA-256 digest.
The enhanced arm linked six of six handoffs and used one plan update per task.
These fixtures check orchestration and persistence. Evaluating the actual
booking, news and fund deliverables still requires the real-model comparison.

## Rules for whoever builds these

From `AGENTS.md` and how the recent changes were made:

1. **Read the upstream code first.** Each proposal lists pinned links. Read the functions, tests and
   error handling they point to; cite them in the commit message and in `docs/architecture.md`
   ("Upstream designs adopted"), with what was adopted and what differs.
2. **Work on a branch in its own worktree** from `jinyuwu/model-driven-studio`; other agents commit
   to that branch. Do not touch the uncommitted AOCI files (`aoci*.txt`, `.aoci/`) or `.gitignore`.
3. **Tests go through the normal entry** (`Studio.post`, the HTTP API, Playwright for UI), with the
   offline `FakeProvider`/`Script` helpers in `tests/helpers.py`. Run the full suite:
   `.venv/bin/python -m unittest discover -s tests -t .` (344 tests today).
4. **Every feature has an `MA_*` switch** so it can be turned off and compared.
5. **Report three things separately** when done: platform regression (the suite), real-model runs
   (`python -m miniagent bench --out runs/bench-<date> --arms team,team-classic --tasks 1`), and
   original-task acceptance (a blind quality check of the deliverables, as done for the 22 Nov run
   in `../MultiAgentNLP-resume/runs/bench-20261122-task1/`). A feature stays on only if quality does not drop.
6. **Use the meter.** Task records now carry `metrics.time` (model/tool time, tokens, cost estimate);
   the bench summary has model/tool minutes and estimated cost. Compare before and after.
