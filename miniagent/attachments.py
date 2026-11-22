"""Immutable message attachments, exposed to agents within the receiving room."""
import hashlib
import json
import re
from pathlib import Path
from .roles import write_json
from .tools import Tool, ToolResult

LIMIT = 20 * 1024 * 1024


class Uploads:
    def __init__(self, root):
        self.root = Path(root) / "uploads"

    def save(self, name, data, key=""):
        self._name(name)
        if len(data) > LIMIT:
            raise ValueError("Attachment exceeds 20 MiB")
        if key and key.encode() in data:
            raise ValueError("Attachment contains the configured API key")
        digest = hashlib.sha256(data).hexdigest()
        folder = self.root / digest
        folder.mkdir(parents=True, exist_ok=True)
        original = folder / "original"
        # Concurrent uploads of the same digest publish identical bytes.
        if not original.exists():
            from tempfile import NamedTemporaryFile
            with NamedTemporaryFile(dir=folder, delete=False) as f:
                f.write(data)
            Path(f.name).replace(original)
        meta = {"id": digest, "name": name, "size": len(data)}
        if not (folder / "meta.json").exists():
            write_json(folder / "meta.json", meta)
        return meta

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not name or len(name) > 255 or Path(name).name != name or name in (".", "..") or any(ord(c)<32 for c in name):
            raise ValueError("Invalid attachment name")
        return name

    def ref(self, data):
        identity = data["id"]
        if not re.fullmatch("[0-9a-f]{64}", identity) or not (self.root / identity / "original").is_file():
            raise ValueError("Unknown upload")
        meta = json.loads((self.root / identity / "meta.json").read_bytes())
        return {**meta,"name":self._name(data.get("name",meta["name"]))}

    def save_in_room(self, store, name, data, key=""):
        return {**Uploads(store.root).save(name, data, key), "session_id":store.group.id}

    def room_ref(self, store, data):
        scope = data.get("session_id")
        if scope is None and not store.parent:
            return self.ref(data)  # Existing uploads and pre-session clients.
        if scope != store.group.id:
            raise ValueError("Attachment belongs to another conversation")
        return {**Uploads(store.root).ref(data), "session_id":scope}

    def original(self, store, ref):
        checked = self.room_ref(store, ref)
        root = store.root / "uploads" if "session_id" in checked else self.root
        return root / checked["id"] / "original"

    def tools(self, store, agent, through_seq=None):
        def available():
            return {r["id"]: r for event in store.chat() for r in event.get("attachments", [])
                    if through_seq is None or event["seq"] <= through_seq}

        def read(ctx, args, copy=False):
            identity = args.get("id", "")
            ref = available().get(identity)
            if not ref:
                return ToolResult.failure("Unknown attachment in this conversation")
            p = self.original(store, ref)
            if copy:
                target = ctx.sandbox.resolve(args["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as out, p.open("rb") as inp:
                    import shutil
                    shutil.copyfileobj(inp, out)
                return ToolResult.success(str(args["path"]))
            start = max(0, int(args.get("offset", 0)))
            with p.open("rb") as f:
                f.seek(start)
                return ToolResult.success(f.read(8000).decode("utf-8", "replace"))

        return [
            Tool("list_attachments", "List files sent to this conversation.", {"type":"object","properties":{}},
                 lambda c,a: ToolResult.success(json.dumps(list(available().values()), ensure_ascii=False))),
            Tool("read_attachment", "Read UTF-8 text at a byte offset.", {"type":"object","properties":{
                 "id":{"type":"string"},"offset":{"type":"integer","minimum":0}},"required":["id"]}, read),
            Tool("copy_attachment_to_workspace", "Copy an attachment to a new file in your workspace.",
                 {"type":"object","properties":{"id":{"type":"string"},"path":{"type":"string"}},"required":["id","path"]},
                 lambda c,a: read(c,a,True))]
