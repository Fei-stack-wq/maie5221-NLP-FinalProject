"""Private continuation state for a cooperatively paused Studio task."""
import json
import os
import tempfile
import threading
from pathlib import Path


class PauseRequested(Exception):
    """Unwind to the Studio worker after each active agent has captured its frame."""


class Continuation:
    def __init__(self, saved=None):
        self.requested = threading.Event()
        self.saved = saved or {}
        self.frames = self.saved.get("frames", {})

    def boundary(self):
        if self.requested.is_set():
            raise PauseRequested()


def path_for(store, task_id):
    return store.task_dir(task_id) / "continuation.json"


def save(path, data):
    # mkstemp keeps model history private. Publish the complete file before advertising Pause.
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".continuation-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version":1, **data}, f, ensure_ascii=False, separators=(",",":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load(path, task_id):
    data = json.loads(path.read_bytes())
    if data.get("version") != 1 or data.get("task_id") != task_id or not data.get("frames"):
        raise ValueError("无法识别此任务的断点。")
    return data
