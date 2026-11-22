"""Synchronize only a user's explicitly bound project with a team's published files."""
from pathlib import Path
import json
import subprocess
import sys
from .roles import write_json
from .workspace import scan, _copy


def pick():
    if sys.platform != "darwin":
        raise ValueError("Enter the local folder path on this platform.")
    result = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "选择团队工作区")'],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def bind(store, raw):
    root = Path(raw).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("Choose an existing absolute directory")
    root = root.resolve()
    runs = store.root.parents[1].resolve()
    if root == Path.home() or root == runs or root in runs.parents or runs in root.parents:
        raise ValueError("Choose a project outside Studio's data directory")
    store.group.folder = str(root)


def checked(root, rel):
    target = root / rel
    if target.is_symlink() or target.resolve() != target or root not in target.parents:
        raise ValueError("Workspace path changed: " + rel)
    return target


def sync(store, *, importing, key=""):
    folder = store.group.folder
    if not folder:
        return []
    external = Path(folder)
    if not external.is_dir() or external.is_symlink() or external.resolve() != external:
        raise ValueError("Bound folder moved or is unavailable")
    project = store.project.root.resolve()
    state = store.root / "folder-sync.json"
    baseline = json.loads(state.read_bytes()) if state.exists() else {}
    if baseline.get("folder") != folder:
        baseline = {}
    previous = baseline.get("files", {})
    outside, skipped = scan(external)
    inside = store.project.manifest()
    warnings = list(skipped)
    updated = dict(previous)
    conflicts = dict(baseline.get("conflicts", {}))
    generated = set(baseline.get("conflict_files", []))
    for name in sorted(outside.keys() | inside.keys() | previous.keys()):
        if name in generated:
            continue
        ext, obj, old = outside.get(name), inside.get(name), previous.get(name)
        if importing:
            if ext == old or (obj != old and obj != ext):
                continue
            src, dst, sha = external, project, ext
        else:
            if obj == old or obj == ext:
                if obj == ext:
                    updated[name] = obj
                continue
            src, dst, sha = project, external, obj
        src_path = checked(src, name)
        dest = checked(dst, name)
        if sha and key and key.encode() in src_path.read_bytes():
            warnings.append(name + " (configured credential excluded)")
            continue
        if not importing and ext != old:
            warnings.append(name + " (external edit preserved)")
            if conflicts.get(name) == [ext,obj]:
                continue
            conflicts[name] = [ext,obj]
            if obj:
                conflict = dest.with_name(dest.name + ".miniagent-conflict")
                index = 1
                while conflict.exists() or conflict.is_symlink():
                    conflict = dest.with_name(dest.name + f".miniagent-conflict-{index}")
                    index += 1
                _copy(src_path, conflict)
                generated.add(conflict.relative_to(external).as_posix())
            continue
        if sha:
            _copy(src_path, dest)
        elif dest.is_file():
            dest.unlink()
        conflicts.pop(name, None)
        updated[name] = sha
    write_json(state, {"folder":folder, "files":{k:v for k,v in updated.items() if v},
                       "conflicts":conflicts, "conflict_files":sorted(generated)})
    return warnings
