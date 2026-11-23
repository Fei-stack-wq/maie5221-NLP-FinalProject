"""Continue interrupted work from durable evidence when no execution frame survived."""
import json
import shutil
import tempfile
from collections import deque
from pathlib import Path

from .schemas import ToolResult
from .tools import Tool
from .workspace import scan


def eligible(room, record):
    return (room.group.kind != "assistant" and record["status"] == "interrupted"
            and isinstance(record.get("text"), str)
            and (not room.cleared_seq or record.get("trigger_seq", 0) > room.cleared_seq))


def lineage(room, record):
    """Walk local continuation links, newest first, respecting cleared history."""
    seen = set()
    while record["id"] not in seen and eligible(room, record):
        yield record
        seen.add(record["id"])
        source = record.get("continued_from")
        if not source:
            break
        try:
            record = room.load_task(source)
        except FileNotFoundError:
            break


def handoff(room, record, agent=None):
    sources = {r["id"] for r in lineage(room, record)}
    events = [e for e in room.chat() if e.get("task_id") in sources]
    pending = {}
    reports = []
    for e in events:
        if e["kind"] == "delegation" and e.get("reconcile"):
            # A follow-up check after parallel work: the original assignment stays the outstanding one.
            if e["assignee"] in pending:
                pending[e["assignee"]]["follow_up_check_started"] = True
        elif e["kind"] == "delegation":
            pending[e["assignee"]] = {k:e[k] for k in ("task_id","assignee","task","deliverables","checks") if k in e}
        elif e["kind"] == "report":
            pending.pop(e["agent"], None)
            reports.append({"agent":e["agent"], "status":e["status"], "text":e.get("text","")[:1600]})
    trace = room.task_dir(record["id"]) / "trace.jsonl"
    tail = deque(maxlen=12)
    if trace.is_file():
        with trace.open() as stream:
            for line in stream:
                try:
                    e = json.loads(line)
                except ValueError:  # An abrupt exit can leave a partial final line.
                    continue
                if e.get("type") in ("step", "tool_call", "tool_result"):
                    tail.append({**{k:e[k] for k in ("type","agent","step","tool","ok","call_id") if k in e},
                                 **({"preview":e["preview"][:800]} if e.get("agent") == agent and isinstance(e.get("preview"),str) else {})})
    inputs = {a["id"]:a for e in events for a in e.get("attachments", [])}
    return {"original_attachments":list(inputs.values()),
            "plan":record.get("plan"), "outstanding_assignments":list(pending.values()),
            "member_reports":reports[-8:], "last_recorded_activity":list(tail)}


def preserve_drafts(room, record, handover):
    """Save participating roles' unpublished files before a checkout can replace them."""
    target = room.task_dir(record["id"]) / "private-drafts"
    if target.is_dir():
        return
    shared = room.project.manifest()
    owners = {a["assignee"] for a in handover["outstanding_assignments"] if a["task_id"] == record["id"]}
    owners.add(record.get("primary", room.group.lead))
    with tempfile.TemporaryDirectory(dir=target.parent) as temp:
        stage = Path(temp) / "drafts"
        stage.mkdir()
        for agent in owners.intersection(m.id for m in room.group.members):
            workspace = room.workspace(agent)
            for path, digest in scan(workspace)[0].items():
                if shared.get(path) != digest:
                    dest = stage / agent / path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(workspace / path, dest)
        stage.rename(target)


def draft_tool(room, source, agent):
    drafts = {}
    for old in lineage(room, room.load_task(source)):
        root = room.task_dir(old["id"]) / "private-drafts" / agent
        for path in root.rglob("*"):
            if path.is_file():
                drafts.setdefault(path.relative_to(root).as_posix(), path)
    if not drafts:
        return None

    def copy(ctx, args):
        path = args["path"]
        if path not in drafts:
            return ToolResult.failure("Unknown private draft.")
        dest = ctx.sandbox.resolve(args["destination"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        with drafts[path].open("rb") as src, dest.open("xb") as out:
            shutil.copyfileobj(src, out)
        return ToolResult(ok=True, content=f"Copied {path} to {args['destination']}.")

    return Tool("copy_recovery_draft", "Retrieve one of your own unpublished files from the interrupted task into a new workspace file.",
                {"type":"object","properties":{"path":{"type":"string","enum":sorted(drafts)},"destination":{"type":"string"}},
                 "required":["path","destination"],"additionalProperties":False}, copy)
