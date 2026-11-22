"""Synchronize only a user's explicitly bound project with a team's published files."""
from pathlib import Path
import json
import subprocess
import sys
from .roles import write_json
from .workspace import scan, _copy


def pick():
    if sys.platform == "darwin":
        command = ["/usr/bin/osascript", "-e", 'POSIX path of (choose folder with prompt "选择项目工作区")']
    else:
        command = [sys.executable, "-c",
                   "from tkinter import Tk, filedialog\n"
                   "root = Tk()\nroot.withdraw()\n"
                   "print(filedialog.askdirectory(parent=root, title='选择项目工作区', mustexist=True))\n"
                   "root.destroy()"]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError:
        raise ValueError("无法打开系统选择器，请输入本机文件夹的绝对路径。") from None
    if result.returncode == 0:
        return result.stdout.rstrip("\r\n")
    if sys.platform == "darwin" and "(-128)" in result.stderr:
        return ""
    raise ValueError("无法打开系统选择器，请输入本机文件夹的绝对路径。")


def bind(store, raw):
    root = Path(raw).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("Choose an existing absolute directory")
    root = root.resolve()
    runs = (store.parent or store).root.parents[1].resolve()
    if root == Path.home() or root == runs or root in runs.parents or runs in root.parents:
        raise ValueError("Choose a project outside Studio's data directory")
    store.group.folder = str(root)


def checked(root, rel):
    target = root / rel
    if target.is_symlink() or target.resolve() != target or root not in target.parents:
        raise ValueError("Workspace path changed: " + rel)
    return target


def sync(store, *, importing, key="", published=None):
    folder = store.group.folder
    if not folder:
        return []
    external = Path(folder)
    if not external.is_dir() or external.is_symlink() or external.resolve() != external:
        raise ValueError("Bound folder moved or is unavailable")
    project = store.project.root.resolve()
    state = store.folder_state
    baseline = json.loads(state.read_bytes()) if state.exists() else {}
    first_import = baseline.get("folder") != folder
    if first_import:
        baseline = {}
    previous = baseline.get("files", {})
    outside, skipped = scan(external)
    inside = store.project.manifest()
    warnings = list(skipped)
    updated = dict(previous)
    conflicts = dict(baseline.get("conflicts", {}))
    generated = set(baseline.get("conflict_files", []))
    names = outside.keys() | inside.keys() | previous.keys() if published is None else published
    for name in sorted(names):
        if name in generated:
            continue
        ext, obj, old = outside.get(name), inside.get(name), previous.get(name)
        if importing:
            if ext == old or (not first_import and obj != old and obj != ext):
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
