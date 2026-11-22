"""Exclusive ownership of a writable Studio data directory for the service lifetime."""
from contextlib import contextmanager
from http.client import HTTPConnection, HTTPException
import json
import time
import os

from .roles import write_json


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


@contextmanager
def advertised_studio(directory, port, instance_id):
    """Publish the actual main listener while the server owns the data lock."""
    record = directory / ".studio-instance.json"
    write_json(record, {"port": port, "id": instance_id})
    try:
        yield
    finally:
        record.unlink(missing_ok=True)


def running_studio_url(directory):
    """Only reconnect to the loopback listener that registered for this directory."""
    record = directory / ".studio-instance.json"
    try:
        owner = json.loads(record.read_bytes())
        port, instance_id = owner["port"], owner["id"]
        if type(port) is not int or not 0 < port <= 65535 or not isinstance(instance_id, str) or len(instance_id) != 32:
            return None
        connection = HTTPConnection("127.0.0.1", port, timeout=0.5)
        try:
            connection.request("GET", "/api/instance")
            response = connection.getresponse()
            if response.status == 200 and json.loads(response.read(256)) == {"id": instance_id}:
                return f"http://127.0.0.1:{port}/"
        finally:
            connection.close()
    except (OSError, ValueError, KeyError, TypeError, HTTPException):
        pass
    return None


def wait_for_studio(directory):
    # A simultaneous double-click can encounter the lock just before serve() publishes its port.
    deadline = time.monotonic() + 3
    while True:
        url = running_studio_url(directory)
        if url or time.monotonic() >= deadline:
            return url
        time.sleep(0.1)
