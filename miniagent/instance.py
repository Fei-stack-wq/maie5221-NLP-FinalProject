"""Exclusive ownership of a writable Studio data directory for the service lifetime."""
from contextlib import contextmanager
import os


class StudioInUse(RuntimeError):
    pass


@contextmanager
def session_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".studio.lock").open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise StudioInUse(f"Studio is already using {directory.resolve()}. Choose a different --runs-dir.") from exc
        yield
