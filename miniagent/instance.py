"""Exclusive ownership of a writable Studio data directory for the service lifetime."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
from http.client import HTTPConnection, HTTPException
import json
import time
import os

from .roles import write_json


PACKAGE = Path(__file__).resolve().parent


def runtime_version(package=PACKAGE):
    """Identify the code and assets actually shipped, including uncommitted edits."""
    digest = hashlib.sha256()
    paths = list(package.rglob("*.py")) + list((package / "web").rglob("*"))
    for path in sorted(set(paths)):
        if path.is_file() and package in path.resolve().parents:
            digest.update(path.relative_to(package).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


@dataclass(frozen=True)
class RegisteredStudio:
    port: int
    id: str
    source: str | None
    version: str | None
    busy: bool

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"


class RestartStudio(Exception):
    """The idle server has drained and wants a fresh interpreter on this port."""


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
def advertised_studio(directory, port, instance_id, version):
    """Publish the actual main listener while the server owns the data lock."""
    record = directory / ".studio-instance.json"
    marker = directory / (".studio-update-" + instance_id)
    write_json(record, {"port": port, "id": instance_id, "source": str(PACKAGE), "version": version})
    try:
        yield marker
    finally:
        marker.unlink(missing_ok=True)
        record.unlink(missing_ok=True)


def registered_studio(directory):
    """Only reconnect to the loopback listener that registered for this directory."""
    record = directory / ".studio-instance.json"
    try:
        owner = json.loads(record.read_bytes())
        port, instance_id = owner["port"], owner["id"]
        if type(port) is not int or not 0 < port <= 65535 or not isinstance(instance_id, str) or not re.fullmatch(r"[0-9a-f]{32}", instance_id):
            return None
        connection = HTTPConnection("127.0.0.1", port, timeout=0.5)
        try:
            connection.request("GET", "/api/instance")
            response = connection.getresponse()
            if response.status == 200 and json.loads(response.read(256)) == {"id": instance_id}:
                return RegisteredStudio(port, instance_id, owner.get("source"), owner.get("version"),
                                        response.getheader("X-Studio-Busy") == "1")
        finally:
            connection.close()
    except (OSError, ValueError, KeyError, TypeError, HTTPException):
        pass
    return None


def wait_for_instance(directory, *, previous=None, timeout=3):
    # The data lock spans listener setup and teardown. Wait for registration by the owner.
    deadline = time.monotonic() + timeout
    while True:
        owner = registered_studio(directory)
        if owner and owner.id != previous:
            return owner
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def running_studio_url(directory):
    owner = registered_studio(directory)
    return owner.url if owner else None


def wait_for_studio(directory):
    owner = wait_for_instance(directory)
    return owner.url if owner else None
