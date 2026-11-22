"""Read-only view of the current conversation's selected workspace."""
from __future__ import annotations

import codecs
import os
import stat
from pathlib import Path

from .workspace import MAX_FILE_BYTES, SKIP_DIRS, SKIP_FILES


_EXCLUDED = SKIP_DIRS | SKIP_FILES
_PAGE_ENTRIES = 48
_EXCERPT_BYTES = 8192


class RoomWorkspace:
    def __init__(self, store, key=""):
        self.store = store
        self.folder = store.group.folder
        self.root = Path(self.folder) if self.folder else store.project.root.resolve()
        self.key = key.encode()

    def describe(self):
        return {"kind": "selected_folder" if self.folder else "studio_project",
                "location": self.folder or ".", "access": "read_only",
                "root_listing": self.inspect(".")}

    def inspect(self, path, offset=0):
        """Offsets count entries for directories and bytes for text files."""
        if not isinstance(path, str) or not path or type(offset) is not int or offset < 0:
            return {"error": "A relative path and a nonnegative offset are required."}
        name = Path(path)
        if name.anchor or ".." in name.parts or any(p in _EXCLUDED for p in name.parts):
            return {"error": "Path is outside the visible workspace."}
        try:
            if self.store.group.folder != self.folder or self.root.resolve() != self.root:
                return {"error": "Workspace binding changed or is unavailable."}
            target = self.root / name
            if target.resolve() != target:
                return {"error": "Linked paths are excluded from this workspace."}
            info = target.stat()
            if stat.S_ISDIR(info.st_mode):
                return self._directory(target, offset)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                return {"error": "File is not available in the project checkout."}
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
            with os.fdopen(os.open(target, flags), "rb") as stream:
                if not os.path.samestat(info, os.fstat(stream.fileno())) or target.resolve() != target:
                    return {"error": "Workspace file changed while opening."}
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES or (self.key and self.key in data):
                return {"error": "File excluded from workspace reads."}
            if b"\0" in data[:_EXCERPT_BYTES]:
                return {"error": "Binary file. A task can inspect its format using workspace tools."}
            end = min(len(data), offset + _EXCERPT_BYTES)
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            text = decoder.decode(data[offset:end], final=end == len(data))
            end -= len(decoder.getstate()[0])
            return {"path": path, "text": text,
                    "next_offset": end if end < len(data) else None}
        except (OSError, ValueError, RuntimeError):
            return {"error": "Workspace path is missing or unreadable."}

    def _directory(self, target, offset):
        entries = []
        with os.scandir(target) as items:
            for item in items:
                if item.name in _EXCLUDED or item.is_symlink():
                    continue
                if item.is_dir(follow_symlinks=False):
                    entries.append(item.name + "/")
                elif item.is_file(follow_symlinks=False) and item.stat(follow_symlinks=False).st_size <= MAX_FILE_BYTES:
                    entries.append(item.name)
        entries.sort()
        end = offset + _PAGE_ENTRIES
        return {"path": target.relative_to(self.root).as_posix(), "entries": entries[offset:end],
                "total": len(entries), "next_offset": end if end < len(entries) else None}
