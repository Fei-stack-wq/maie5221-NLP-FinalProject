"""Scoped tools for Studio conversations, using the same service as the UI."""
import json
from . import studio_dispatch
from .tools import Tool, ToolResult, validate_arguments


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


SETTINGS_FIELDS = {
    "group_id":STRING, "name":STRING, "folder":{"type":"string","description":"Absolute local folder, or empty to unbind."},
    "mode":{"type":"string","enum":["interactive","autonomous"]}, "pinned":{"type":"boolean"}, "archived":{"type":"boolean"},
}
SETTINGS_DESCRIPTION = "Update a team's or conversation's name, workspace folder, interaction mode, pin or archive status. Address a session for its own name and folder."
CANCEL_DESCRIPTION = "Stop exactly one running or queued assignment by task ID. A stopping receipt means cancellation is in progress."
INSPECT_DESCRIPTION = "Look up one Studio conversation's settings, members and tasks, newest first. Use its ID from the directory."
TASK_OPTIONS = {"type":"object", "additionalProperties":False, "properties":{
    "thinking":{"type":"string","enum":["enabled","disabled"]},
    "interaction_mode":{"type":"string","enum":["interactive","autonomous"]},
    **{k:{"type":"integer"} for k in ("lead_max_steps","member_max_steps","max_delegations")},
    "checks":{"type":"array","items":{"type":"object","properties":{
        "kind":{"type":"string","enum":["exists","contains","command"]},"path":STRING,"text":STRING,"command":STRING},
        "required":["kind"]}},
}}
TARGET_FIELDS = {"group_id":STRING, "text":STRING, "assignee":STRING, "options":TASK_OPTIONS,
                 "attachment_ids":{"type":"array","items":STRING,"description":"Original attachments in this assistant chat to forward."}}


def update_settings(studio, group_id, **changes):
    if not changes or changes.keys() - (SETTINGS_FIELDS.keys() - {"group_id"}):
        raise ValueError("Specify supported settings to change.")
    return studio.update_group(group_id, changes)


def assistant_tools(studio, source_id=None, task_id=None):
    # Older running workers can still lazy-load this module.
    linked = bool(source_id and task_id and hasattr(studio, "await_dispatch"))
    origin = {"group_id":source_id, "task_id":task_id}
    def create_group(name, role_ids, lead, kind="group"):
        if kind not in ("group","dm"):
            raise ValueError("Choose group or dm.")
        options = {"created_by":origin} if linked else {}
        return studio.create_group(name, [], lead, role_ids=role_ids, kind=kind, **options)

    def dispatch(group_id, text, assignee=None, attachment_ids=(), options=None):
        if not linked:
            return studio.post(group_id, text, queue=True, **({"dispatched_by":origin} if task_id and source_id else {}))
        source = studio.group(source_id)
        if (not isinstance(attachment_ids,(list,tuple)) or len(attachment_ids)>20
                or not all(isinstance(i,str) for i in attachment_ids)):
            raise ValueError("Supply up to 20 attachment IDs.")
        available = {r["id"]:r for e in source.chat() for r in e.get("attachments", [])}
        inputs = [(source, available[i]) for i in dict.fromkeys(attachment_ids)]
        return studio_dispatch.submit(studio, origin, group_id, text, assignee=assignee, inputs=inputs, options=options)

    def batch(assignments):
        if not isinstance(assignments,list) or not assignments:
            raise ValueError("Supply a nonempty list of assignments.")
        results = []
        for item in assignments:
            try:
                issues = validate_arguments({"properties":TARGET_FIELDS, "required":["group_id","text"]},item)
                if issues:
                    raise ValueError(", ".join(issues))
                results.append(dispatch(**item))
            except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
                results.append({"group_id":item.get("group_id") if isinstance(item,dict) else None, "error":str(exc)})
        complete = all("task_id" in row for row in results)
        return ToolResult(ok=complete, content=json.dumps(results,ensure_ascii=False), handed_off=complete)

    def start_task(group_id, text, after_dispatch="handoff", assignee=None, attachment_ids=(), options=None):
        if after_dispatch not in ("handoff", "continue", "await_result"):
            raise ValueError("Unknown dispatch behavior")
        group = studio.group(group_id)
        if group.group.kind == "assistant":
            raise ValueError("Choose a group or a DM")
        receipt = dispatch(group_id, text, assignee, attachment_ids, options)
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
            {**{k:r[k] for k in ("id","name","kind","running","archived","lead","team_id","team_name","session_name")},
             "members":[m["id"] for m in r["members"]]} for r in studio.conversations() if r["kind"]!="assistant"]),
        action("create_role", "Create a reusable role.", lambda **data: studio.roles.save(data),
               {k:STRING for k in ("id","name","prompt")}, ("id","name","prompt")),
        action("create_group", "Create a conversation with library roles.", create_group,
               {"name":STRING, "role_ids":{"type":"array","items":STRING},"lead":STRING,
                "kind":{"type":"string","enum":["group","dm"]}}, ("name","role_ids","lead")),
        action("start_task", "Dispatch to an existing conversation by its id (a group id addresses the default session), or a DM. Studio immediately posts links here and automatically "
               "returns the outcome and files here. By default the handoff finishes your turn. "
               "Choose continue if you have independent work left now, or await_result if you need the child's "
               "result for a subsequent step. Waiting is handled by Studio.", start_task,
               {**TARGET_FIELDS,
                "after_dispatch":{"type":"string","enum":["handoff","continue","await_result"]}}, ("group_id","text")),
        action("task_status", "Read a dispatched task's status, final answer and published changes.",
               studio.dispatch_status, {"task_id":STRING}, ("task_id",)),
        action("read_group_file", "Read a published project file by byte offset.", file,
               {"group_id":STRING,"path":STRING,"offset":{"type":"integer","minimum":0}}, ("group_id","path"))]
    # A still-running Studio may lazily load this module before its service has been restarted.
    if hasattr(studio, "create_session"):
        tools.append(action("create_session", "Open a separate conversation and workspace under an existing group with its shared roster.",
                            studio.create_session, {"group_id": STRING, "name": STRING}, ("group_id", "name")))
    if hasattr(studio, "change_members"):
        tools.append(action("change_group_members", MEMBERSHIP_DESCRIPTION, studio.change_members,
                            MEMBERSHIP_FIELDS, ("group_id",)))
    if linked:
        tools.extend([
            action("dispatch_tasks", "Send several independent briefs to their respective conversations. Each accepted assignment returns its own receipt. Retry missing assignments after a partial failure.", batch,
                   {"assignments":{"type":"array","items":{"type":"object","properties":TARGET_FIELDS,"required":["group_id","text"],"additionalProperties":False}}}, ("assignments",)),
            action("inspect_studio_room", INSPECT_DESCRIPTION, lambda **args:studio_dispatch.inspect_room(studio, **args),
                   {"group_id":STRING, "offset":{"type":"integer","minimum":0}}, ("group_id",)),
            action("cancel_task", CANCEL_DESCRIPTION, lambda task_id:studio_dispatch.cancel_task(studio, task_id),
                   {"task_id":STRING}, ("task_id",)),
            action("redispatch_task", "Reassign or rerun a recorded task in a target conversation. Carries its original brief, inputs and checks. Optionally replace text or name an assignee. Once accepted, stop the old execution if still active.",
                   lambda **args:studio_dispatch.redispatch(studio, origin, **args),
                   {"task_id":STRING,"group_id":STRING,"text":STRING,"assignee":STRING}, ("task_id","group_id")),
            action("update_group_settings", SETTINGS_DESCRIPTION, lambda **args:update_settings(studio, **args),
                   SETTINGS_FIELDS, ("group_id",)),
            action("update_role", "Edit a library role's name and instructions for future assignments.",
                   lambda **data:studio.roles.save(data, update=True), {k:STRING for k in ("id","name","prompt")}, ("id",)),
            action("list_group_files", "List one conversation's published files.", lambda group_id:studio.group(group_id).project.files(),
                   {"group_id":STRING}, ("group_id",)),
            action("role_knowledge_history", "Read the audit history and available revisions for a library role.",
                   studio.roles.history, {"role":STRING}, ("role",)),
            action("list_role_knowledge", "Inspect a role's memories, skills and review statuses.", studio.roles.entries, {"role":STRING}, ("role",)),
            action("propose_role_knowledge", "Propose a memory or skill for a library role.", studio.roles.propose,
                   {"role":STRING,"text":STRING,"scope":STRING,"kind":STRING}, ("role","text","scope")),
            action("review_role_knowledge", "Apply the user's knowledge review decision, or restore a named revision.", studio.roles.revise,
                   {"role":STRING,"identity":STRING,"status":{"type":"string","enum":["approved","rejected","archived","restore"]},
                    "text":STRING,"revision":{"type":"integer"}}, ("role","identity","status")),
            action("trash_conversation", "Move a conversation to Studio trash when the user requests deletion.", studio.trash,
                   {"gid":STRING}, ("gid",)),
        ])
    return tools
