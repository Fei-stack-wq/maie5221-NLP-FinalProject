"""Reproducible source identity without relying on an installed Git checkout."""
import hashlib
import importlib.metadata
import platform
from pathlib import Path


def runtime_identity():
    root = Path(__file__).parent
    hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
              for p in sorted(root.rglob("*.py"))}
    packages = {}
    for package in ("pydantic", "psutil", "Pillow", "playwright"):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
    import json
    return {"python": platform.python_version(), "platform": platform.system(), "packages": packages,
            "source_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
            "source_files": hashes}
