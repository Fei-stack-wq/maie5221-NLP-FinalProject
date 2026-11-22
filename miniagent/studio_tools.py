"""Scoped tools for Studio conversations, using the same service as the UI."""
import json
from .tools import Tool, ToolResult


def action(name, description, fn, fields=None, required=()):
    fields = fields or {}
    def invoke(ctx, args):
        try:
            value = fn(**args)
            return value if isinstance(value, ToolResult) else ToolResult.success(json.dumps(value, ensure_ascii=False))
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
            return ToolResult.failure(str(exc))
    return Tool(name, description, {"type":"object","properties":fields,"required":list(required),
                                    "additionalProperties":False}, invoke)


STRING = {"type": "string"}


def role_tools(studio, group, role):
    scope = "group:" + group.id
    registered = role in group.role_ids
    def propose(text, scope_type="group", kind="memory"):
        if scope_type == "general" and not registered:
            raise ValueError("General knowledge requires a library role")
        return studio.roles.propose(role, text, "general" if scope_type == "general" else scope, kind)

    def recall(query=""):
        return [r for r in studio.roles.entries(role, group=group.id)
                if (registered or r["scope"] == scope) and query.casefold() in r["text"].casefold()][-25:]

    return [action("propose_knowledge", "Propose a note or skill for the user to approve.", propose,
                   {"text":STRING,"scope_type":{"enum":["general","group"],"type":"string"},
                    "kind":{"enum":["memory","skill"],"type":"string"}}, ["text"]),
            action("recall_knowledge", "Find approved knowledge visible to this role in this conversation.",
                   recall, {"query":STRING})]


MEMBERSHIP_FIELDS = {
    "group_id":STRING, "add_role_ids":{"type":"array","items":STRING},
    "remove_member_ids":{"type":"array","items":STRING}, "lead":STRING,
}
MEMBERSHIP_DESCRIPTION = ("Change an existing group's roster. Add library roles, remove members by ID, "
                          "or set the lead. Supply a remaining lead if removing the incumbent.")


def assistant_tools(studio, source_id=None, task_id=None):
    # Older running workers can still lazy-load this module.
    linked = bool(source_id and task_id and hasattr(studio, "await_dispatch"))
    origin = {"group_id":source_id, "task_id":task_id}
    def create_group(name, role_ids, lead, kind="group"):
        options = {"created_by":origin} if linked else {}
        return studio.create_group(name, [], lead, role_ids=role_ids, kind=kind, **options)

    def start_task(group_id, text, after_dispatch="handoff"):
        if after_dispatch not in ("handoff", "continue", "await_result"):
            raise ValueError("Unknown dispatch behavior")
        group = studio.group(group_id)
        if group.group.kind == "assistant":
            raise ValueError("Choose a group or a DM")
        options = {"dispatched_by":origin} if source_id and task_id else {}
        receipt = studio.post(group_id, text, queue=True, **options)
        if not linked:
            return receipt
        if after_dispatch == "await_result":
            return studio.await_dispatch(group_id, receipt["task_id"], source_id, task_id)
        return ToolResult(ok=True, content=json.dumps(receipt), handed_off=after_dispatch=="handoff")

    def file(group_id, path, offset=0):
        if offset < 0:
            raise ValueError("Invalid offset")
        with studio.read_file(group_id, path).open("rb") as stream:
            stream.seek(offset)
            return stream.read(8000).decode("utf-8", "replace")

    tools = [
        action("list_roles", "List roles in the user's library.", studio.roles.list),
        action("list_groups", "List the user's conversations.", lambda: [
            {**{k:r[k] for k in ("id","name","kind","running","archived","lead")},
             "members":[m["id"] for m in r["members"]]} for r in studio.groups() if r["kind"]!="assistant"]),
        action("create_role", "Create a reusable role.", lambda **data: studio.roles.save(data),
               {k:STRING for k in ("id","name","prompt")}, ("id","name","prompt")),
        action("create_group", "Create a conversation with library roles.", create_group,
               {"name":STRING, "role_ids":{"type":"array","items":STRING},"lead":STRING,
                "kind":{"type":"string","enum":["group","dm"]}}, ("name","role_ids","lead")),
        action("start_task", "Dispatch to a group or DM. Studio immediately posts links here and automatically "
               "returns the outcome and files here. By default the handoff finishes your turn. "
               "Choose continue if you have independent work left now, or await_result if you need the child's "
               "result for a subsequent step. Waiting is handled by Studio.", start_task,
               {"group_id":STRING, "text":STRING,
                "after_dispatch":{"type":"string","enum":["handoff","continue","await_result"]}}, ("group_id","text")),
        action("task_status", "Read a dispatched task's status, final answer and published changes.",
               studio.dispatch_status, {"task_id":STRING}, ("task_id",)),
        action("read_group_file", "Read a published project file by byte offset.", file,
               {"group_id":STRING,"path":STRING,"offset":{"type":"integer","minimum":0}}, ("group_id","path"))]
    # A still-running Studio may lazily load this module before its service has been restarted.
    if hasattr(studio, "change_members"):
        tools.append(action("change_group_members", MEMBERSHIP_DESCRIPTION, studio.change_members,
                            MEMBERSHIP_FIELDS, ("group_id",)))
    return tools
