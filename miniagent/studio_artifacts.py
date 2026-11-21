"""Attribute project files to the Studio turn that added, changed or requested them."""
from __future__ import annotations

import hashlib
import json
import stat
from functools import lru_cache

from .studio_conversations import thread_info


@lru_cache(maxsize=8192)
def _digest(root, relative, signature):
    # Cache only the digest. The inode and timestamps invalidate it during live
    # execution and when the host replaces a file at the same relative path.
    from .studio_service import _read_file
    data, clipped = _read_file(root, relative, 8 * 1024**2)
    return None if clipped else hashlib.sha256(data).hexdigest()


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _baseline(directory, filename, *, nested=False):
    from .studio_service import _EXTENSIONS, _read_file
    from .studio_workspace import _baseline_files
    data, clipped = _read_file(directory, filename, 4 * 1024**2)
    if clipped:
        raise ValueError("incomplete input manifest")
    value = json.loads(data)
    return _baseline_files(value if nested else {"files": value}, _EXTENSIONS)


def output_classifier(service, path, meta):
    """Compare against the actual checkout, including routed and recovered files."""
    from .studio_service import StudioError
    if "thread" not in meta and not meta.get("workspace_path"):
        # Standalone runs have no inherited checkout. Historical layouts include
        # per-attempt workspaces as well as agents/<role>.
        return None
    turn = thread_info(path.name, meta)
    if not turn:
        return None  # Legacy CLI/orchestration runs use independent per-task trees.
    prefix = "agents/" + turn["recipient"] + "/"
    declared = set(meta.get("request", {}).get("acceptance_files", []))
    inputs, previous, previous_prefix = {}, None, ""
    try:
        if meta.get("studio_dispatch"):
            inputs = _baseline(path, "dispatch-inputs.json")
        elif (path / "inherited-inputs.json").exists():
            inputs = _baseline(path, "inherited-inputs.json")
        elif meta.get("workspace_path") and not turn.get("recovery_source"):
            inputs = _baseline(path, "workspace-baseline.json", nested=True)
        elif turn.get("base_run_id"):
            # Older unbound conversations did not persist an intake manifest.
            # Compare the retained source turn (or the actual recovery checkpoint).
            previous = service._directory(turn["base_run_id"])
            old = thread_info(previous.name, service._meta(previous))
            author = old["recipient"]
            if recovery := turn.get("recovery_source"):
                previous, info = service._select_room_checkpoint(previous, old, recovery["task_id"])
                author = info["role"]
            previous_prefix = "agents/" + author + "/"
            if not (previous / previous_prefix).is_dir():
                # A completed answer without any filesystem operations may have
                # an empty workspace. A missing historical source is ambiguous.
                inputs, previous = None, None
    except (OSError, TypeError, ValueError, KeyError):
        # Never relabel an entire checkout as new because history is unavailable.
        inputs, previous = None, None

    def classify(relative, info):
        if turn["intent"] == "question" or not relative.startswith(prefix):
            return None
        name = relative[len(prefix):]
        if inputs is None:
            return "declared" if name in declared else None
        before = inputs.get(name)
        try:
            if previous is not None:
                source = previous_prefix + name
                try:
                    old_info = (previous / source).stat(follow_symlinks=False)
                except FileNotFoundError:
                    old_info = None
                if old_info is not None:
                    if not stat.S_ISREG(old_info.st_mode) or old_info.st_nlink != 1:
                        return None
                    before = _digest(previous, source, _signature(old_info))
            if before is None:
                return "added"
            if _digest(path, relative, _signature(info)) != before:
                return "modified"
        except (OSError, StudioError):
            # A file can disappear during a live poll. The next poll rescans.
            return None
        return "declared" if name in declared else None

    return classify
