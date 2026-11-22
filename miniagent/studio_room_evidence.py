"""Expose text from a selected, version-bound group delivery to a chat reply."""
from __future__ import annotations

import hashlib
import re

from .project_data import is_database, is_png, validate_project_data
from .schemas import ToolResult
from .studio_conversations import thread_info
from .tools import Tool


def published_artifact_tool(service, room_id, tasks):
    receipts = {(task["run_id"], item["path"]): item for task in tasks
                for item in task.get("published_artifacts", [])}
    if not receipts:
        return None

    def read(_ctx, args):
        from .studio_service import StudioError

        run_id, path = args["run_id"], args["path"]
        item = receipts.get((run_id, path))
        if item is None:
            return ToolResult.failure("该版本与文件的组合不在本群的已发布交付清单中。")
        if is_png(path) or is_database(path):
            return ToolResult.failure("该工件是二进制文件，不能作为文本摘录。")
        offset, limit = args.get("offset", 0), args.get("limit", 4096)
        if offset < 0 or not 1 <= limit <= 32768:
            return ToolResult.failure("摘录范围无效。")
        try:
            directory = service._directory(run_id)
            meta = service._meta(directory)
            thread = thread_info(run_id, meta) or {}
            if thread.get("kind") != "group" or thread.get("id") != room_id:
                return ToolResult.failure("工件不属于当前群。")
            ref = item["ref"]
            digest = item.get("sha256")
            archived = re.fullmatch(r"handoffs/[a-f0-9]{32}/(.+)", ref)
            if archived and archived[1] != path:
                return ToolResult.failure("交接引用与发布的文件名不一致。")
            final = meta.get("workspace_delivery") or {}
            if (meta.get("state") in {"completed", "unverified"} and final.get("status") == "published"
                    and path in final.get("files", [])):
                # The final lead may have edited a file after its member signed a
                # handoff. Only the verified, changed bytes from this run qualify.
                basis = service._projection_basis(directory, initial=True)
                version = service._projection_basis(directory)
                published = version["files"].get(path)
                if not published or published == basis["files"].get(path) or version["directory"] != basis["directory"]:
                    return ToolResult.failure("无法确认该文件属于本轮最终发布的改动。")
                if published != digest or not archived:
                    ref = f"agents/{thread['recipient']}/{path}"
                digest = published
            elif not archived:
                return ToolResult.failure("没有可验证的已发布版本。")
            data, clipped = service._artifact_data(run_id, ref, 8 * 1024**2)
            if clipped or hashlib.sha256(data).hexdigest() != digest:
                return ToolResult.failure("工件与发布时的内容摘要不一致，无法引用。")
            validate_project_data(path, data, secret=service.settings.api_key, reject_credentials=True)
            text = data.decode("utf-8")
        except (StudioError, ValueError, OSError, KeyError):
            return ToolResult.failure("无法验证或预览该版本的文本工件。")
        if offset > len(text):
            return ToolResult.failure("offset 超出了工件正文。")
        if needle := args.get("find"):
            position = text.find(needle, offset)
            if position < 0:
                return ToolResult.failure("这份已发布版本没有出现所搜索的文本。")
            offset = max(offset, position - min(240, limit // 4))
        end = min(offset + limit, len(text))
        return ToolResult.success(text[offset:end], offset=offset, next_offset=end if end < len(text) else None,
                                  artifact_reference={"run_id": run_id, "path": path, "ref": ref})

    schema = {"type": "object", "properties": {
        "run_id": {"type": "string", "description": "工件所属的任务 run_id"},
        "path": {"type": "string", "description": "该任务 published_artifacts 列出的项目相对路径"},
        "offset": {"type": "integer", "minimum": 0, "description": "摘录的字符起点"},
        "limit": {"type": "integer", "minimum": 1, "description": "摘录字符数，默认 4096"},
        "find": {"type": "string", "description": "可选，先在正文中定位这段文字，再读取附近内容"}},
        "required": ["run_id", "path"], "additionalProperties": False}
    return Tool("read_published_artifact", "读取本群可核实的交付版本的文本摘录。", schema, read)
