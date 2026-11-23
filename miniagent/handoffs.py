"""Hand-off kinds, preloaded inputs and evidence for team assignments.

The lead tags a hand-off by the *shape* of the work, not by the domain, so the same five kinds
fit a software build, a newsroom and a fund report:

``research``  find things out (sources, data, an existing codebase)
``make``      produce a deliverable (code, an article, a deck, an analysis)
``check``     independently review or test someone else's work
``fix``       address findings from a check
``adapt``     turn finished, checked work into other material (release notes, a summary, posts)

Studio sizes thinking, effort, step budget and tools by kind (``config.KIND_PROFILES``), as Codex lets
a spawned agent take a role's model and reasoning effort. The files a hand-off declares as ``inputs`` are
included in the assignment, as aider keeps read-only files in the chat instead of letting the model fetch
them one call at a time. check, fix and adapt hand-offs receive what earlier hand-offs reported and
which checks they passed, as CrewAI gives a task the outputs of the tasks in its ``context``.
"""
from __future__ import annotations

from pathlib import Path

PRELOAD_TOTAL = 24_000
"""Characters of input files included in one assignment."""
PRELOAD_EACH = 12_000
PRELOAD_FILES = 30
EVIDENCE_TOTAL = 9_000
"""Characters of earlier hand-off evidence included in one assignment."""
EVIDENCE_EACH = 2_500
EVIDENCE_KEPT = 30

KIND_NOTES = {
    "research": ("Hand-off kind: research. Find out what is true and report it with sources or evidence. Write "
                 "notes the team can build on, not the finished deliverable, unless the assignment asks for it."),
    "make": ("Hand-off kind: make. Produce the deliverable completely. Run the fast checks for what you made (a "
             "test, a command, opening the file); a teammate does the full end-to-end check."),
    "check": ("Hand-off kind: check. You judge someone else's work independently and own its verification: "
              "inspect the real result, run what is needed and report concrete evidence. Keep each browser script "
              "short (about a minute; split a long flow into several calls). Do not fix the work yourself."),
    "fix": ("Hand-off kind: fix. Address the findings given. Change only what they need, re-run the checks that "
            "failed, and report what you changed and how you verified it."),
    "adapt": ("Hand-off kind: adapt. Turn the finished, checked work into the requested material. Rely on the "
              "reports, check results and files given here; do not rebuild or re-test the work (this hand-off has "
              "no browser, and a command stops after a minute)."),
}

EVIDENCE_HEADINGS = {
    "check": ("## Earlier hand-offs in this task\nWhat the authors say they did and verified. Verify it yourself; "
              "do not repeat checks that are listed as passed unless you doubt them."),
    "fix": "## Earlier hand-offs in this task\nIncluding the findings to address.",
    "adapt": ("## Finished work to adapt\nReports, check results and evidence from earlier hand-offs. Rely on "
              "them; do not re-test."),
}


def lead_guidance(*, kinds: bool, background: bool) -> list[str]:
    parts = []
    if kinds:
        parts.append(
            "- Tag each hand-off with `kind`, by the shape of the work rather than the domain: `research` finds "
            "things out (sources, data, an existing codebase); `make` produces a deliverable (code, an article, a "
            "deck, an analysis); `check` independently reviews or tests someone else's work; `fix` addresses "
            "findings from a check; `adapt` turns finished, checked work into other material (release notes, a "
            "summary, social posts, a client introduction). Studio sets thinking, step budget and tools by kind "
            "— only `check` reasons at full effort, `adapt` works without thinking or a browser — and gives "
            "check, fix and adapt hand-offs the earlier reports and check results. List the files a hand-off "
            "builds on in `inputs`: Studio includes their contents, so the teammate starts at once.")
    if background:
        parts.append(
            "- `background: true` starts a hand-off and returns at once, so you can keep working"
            + ("; `adapt` hand-offs run in the background unless you set `background: false`" if kinds else "")
            + ". Its report arrives as a message at your next step after it finishes. Call `wait_handoffs` when "
            "your next step needs a result, or when you have nothing else to do — before your final answer. "
            "While it runs, do not edit, assign or build on the files it produces. Sent in the same response as "
            "other hand-offs, it starts at the same time as them, wherever it appears, as long as it neither reads "
            "nor names what they produce; otherwise the response runs in order.")
    return parts


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    return text[:head] + f"\n…[{len(text) - limit} characters omitted]…\n" + text[-(limit - head):]


def _norm(path: str) -> str:
    return str(path).strip().removeprefix("./").rstrip("/")


def _overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def preload(root: Path, inputs: list[str], total: int = PRELOAD_TOTAL) -> str:
    """The text of a hand-off's input files in its workspace, bounded; folders contribute their files."""
    root = Path(root).resolve()
    files: list[tuple[str, Path]] = []
    for item in inputs:
        rel = _norm(item)
        if not rel:
            continue
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            continue
        if target.is_file():
            files.append((rel, target))
        elif target.is_dir():
            found = sorted(p for p in target.rglob("*") if p.is_file()
                           and not any(part.startswith(".") for part in p.relative_to(root).parts))
            files.extend((p.relative_to(root).as_posix(), p) for p in found)
    seen, parts, left_out, used = set(), [], [], 0
    for rel, path in files:
        if rel in seen:
            continue
        seen.add(rel)
        if len(seen) > PRELOAD_FILES:
            left_out.append(rel)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            left_out.append(f"{rel} (not a text file)")
            continue
        room = min(PRELOAD_EACH, total - used)
        if room < 500:
            left_out.append(rel)
            continue
        body = text if len(text) <= room else (text[:room] + f"\n…[{len(text) - room} more characters: "
                                                "read the rest with read_lines]")
        parts.append(f"=== {rel} ===\n{body}")
        used += len(body)
    if not parts and not left_out:
        return ""
    block = ["## Input files\nTheir contents at the start of this assignment, so you need not read them again."]
    block.extend(parts)
    if left_out:
        block.append("Not included here (read them with read_files if you need them): " + ", ".join(left_out[:40]))
    return "\n\n".join(block)


def evidence_row(*, seq, member, name, kind, status, report, changes, checked, browser, verdict) -> dict:
    row = {"seq": seq, "member": member, "name": name, "kind": kind or "", "status": status,
           "report": _clip(report, EVIDENCE_EACH),
           "files": [f"{c.path} ({c.change})" for c in changes][:30], "paths": [c.path for c in changes][:60]}
    if checked:
        failed = []
        for item in checked["checks"]:
            if item["passed"]:
                continue
            if item["kind"] == "command":
                failed.append(f"command `{_clip(item['command'], 120)}` (exit {item.get('exit_code')})")
            elif item["kind"] == "contains":
                failed.append(f"{item['path']} contains {_clip(item['text'], 60)!r}")
            else:
                failed.append(f"{item['path']} exists")
        row["checks"] = (f"{checked['passed']}/{checked['total']} passed"
                         + (" — failed: " + "; ".join(failed[:6]) if failed else ""))
    if browser:
        row["browser"] = _clip(browser, 1200)
    if verdict and verdict != "unclear":
        row["verdict"] = verdict
    return row


def evidence_block(rows: list[dict], *, kind: str, member: str, continuing: bool,
                   paths: list[str], total: int = EVIDENCE_TOTAL) -> str:
    """Earlier hand-offs for a check, fix or adapt hand-off: related ones first, then the most recent.

    A member continuing its own session already remembers its own reports, so they are left out.
    """
    if kind not in EVIDENCE_HEADINGS:
        return ""
    watched = [_norm(p) for p in paths if _norm(p)]
    candidates = [r for r in rows if not (continuing and r["member"] == member)]
    related = lambda r: any(_overlaps(p, w) for p in r["paths"] for w in watched)  # noqa: E731
    ordered = sorted(candidates, key=lambda r: (not related(r), -r["seq"]))
    parts, used = [], 0
    for row in ordered:
        lines = [f"### {row['name']} ({row['member']}) — {row['kind'] or 'hand-off'} · {row['status']}"
                 + (f" · verdict: {row['verdict']}" if row.get("verdict") else "")]
        lines.append("Published: " + (", ".join(row["files"]) if row["files"] else "no files"))
        if row.get("checks"):
            lines.append("Studio checks: " + row["checks"])
        if row.get("browser"):
            lines.append(row["browser"])
        lines.append(row["report"])
        text = "\n".join(lines)
        if used + len(text) > total:
            if not parts:
                parts.append(_clip(text, total))
            break
        parts.append(text)
        used += len(text)
    if not parts:
        return ""
    return EVIDENCE_HEADINGS[kind] + "\n\n" + "\n\n".join(parts)
