"""Publish verified project changes to a user-selected local directory."""
import hashlib
import os
import stat
import subprocess
import sys
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from string import hexdigits

from .project_data import PROJECT_CACHE_DIRS, SQLITE_SIDECARS, is_database, supported_project_file, validate_project_data


class WorkspaceError(ValueError):
    pass


_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_LIMIT = 8 * 1024**2


def select_root(value, runs):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise WorkspaceError("请输入本机项目文件夹的绝对路径。")
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise WorkspaceError("请选择已存在的本地文件夹。")
    path = path.resolve()
    if (path == Path.home() or path in runs.parents or runs == path or runs in path.parents
            or any(part.startswith(".") for part in path.parts)):
        raise WorkspaceError("请选择独立的项目文件夹。")
    return str(path)


def pick_directory():
    if sys.platform == "darwin":
        try:
            result = subprocess.run(["/usr/bin/osascript", "-e",
                                 'POSIX path of (choose folder with prompt "选择 Agent Studio 项目工作区")'],
                                capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError("目录选择器未返回，可直接填写文件夹路径。") from exc
        if result.returncode == 0:
            return result.stdout.strip() or None
        if "-128" in result.stderr:
            return None
        raise WorkspaceError("无法打开系统目录选择器，可直接填写文件夹路径。")
    try:
        from tkinter import Tk, filedialog
        window = Tk()
        try:
            window.withdraw()
            return filedialog.askdirectory(title="选择 Agent Studio 项目工作区", mustexist=True) or None
        finally:
            window.destroy()
    except Exception as exc:
        raise WorkspaceError("系统目录选择器不可用，可直接填写文件夹路径。") from exc


@contextmanager
def _directory(path):
    with ExitStack() as stack:
        fd = os.open(path.anchor, _DIR)
        stack.callback(os.close, fd)
        for name in path.parts[1:]:
            fd = os.open(name, _DIR, dir_fd=fd)
            stack.callback(os.close, fd)
        yield fd


def _contents(fd, name):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        file = os.open(name, flags, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(file, "rb") as stream:
        info = os.fstat(file)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _LIMIT:
            raise WorkspaceError("工作区文件不是普通文件或超过单文件 8 MiB。")
        data = stream.read(_LIMIT + 1)
        if len(data) != info.st_size or os.fstat(file).st_mtime_ns != info.st_mtime_ns:
            raise WorkspaceError("读取期间项目文件已发生变化，请重试。")
        return data


def _digest(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def _scan(fd, extensions, secret):
    result, total = {}, 0
    def fail(error):
        raise error
    for directory, dirs, files, parent in os.fwalk(".", dir_fd=fd, onerror=fail):
        dirs[:] = sorted(n for n in dirs if not n.startswith(".") and n not in PROJECT_CACHE_DIRS)
        for name in sorted(files):
            if name.startswith("."):
                continue
            if name.lower().endswith(SQLITE_SIDECARS):
                raise WorkspaceError("项目数据库正在使用，请关闭连接并完成 checkpoint。")
            if not supported_project_file(name, extensions):
                continue
            _check_database_idle(parent, name)
            data = _contents(parent, name)
            if data is None:
                raise WorkspaceError("读取期间文件已被移走。")
            _check_database_idle(parent, name)
            relative = (Path(directory) / name).as_posix()
            validate_project_data(relative, data, secret=secret, reject_credentials=True)
            total += len(data)
            if len(result) >= 1000 or total > 20 * 1024**2:
                raise WorkspaceError("工作区超过 1000 个项目文件或总量 20 MiB。")
            result[relative] = data
    return result


def _identity(fd):
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino]


def _project_path(name, extensions):
    return (isinstance(name, str) and "\x00" not in name and "\\" not in name
            and all(part and not part.startswith(".") and part not in PROJECT_CACHE_DIRS for part in name.split("/"))
            and supported_project_file(name, extensions))


def _baseline_files(baseline, extensions):
    # The persisted baseline also lists removed files, so its entries must
    # meet the same project boundaries as names discovered by the new scan.
    files = baseline.get("files") if isinstance(baseline, dict) else None
    if not isinstance(files, dict) or len(files) > 1000:
        raise WorkspaceError("所选工作区的原始文件清单无效。")
    for name, digest in files.items():
        if (not _project_path(name, extensions)
                or not isinstance(digest, str) or len(digest) != 64 or any(c not in hexdigits for c in digest)):
            raise WorkspaceError("所选工作区的原始文件清单无效。")
    return files


def checkout(local, private, extensions, secret):
    with _directory(Path(local)) as root:
        data = _scan(root, extensions, secret)
        baseline = {"directory": _identity(root), "files": {p: _digest(b) for p, b in data.items()}}
    private.mkdir(parents=True, exist_ok=True)
    for name, content in data.items():
        target = private / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return baseline


def _verify_projected(root, paths, proposed):
    for name in sorted(paths):
        parts = Path(name).parts
        with ExitStack() as parents:
            directory = root
            try:
                for part in parts[:-1]:
                    directory = os.open(part, _DIR, dir_fd=directory)
                    parents.callback(os.close, directory)
                leaf = parts[-1]
                _check_database_idle(directory, leaf)
                digest = _digest(_contents(directory, leaf))
                _check_database_idle(directory, leaf)
            except FileNotFoundError:
                digest = None
            if digest != proposed.get(name):
                raise WorkspaceError(f"本机交付文件与已发布版本不符，保留本机修改：{name}。")


def matches_projection(local, basis, name):
    """Verify the directory identity and the bytes of one historically delivered file."""
    try:
        with _directory(Path(local)) as root:
            if _identity(root) != basis["directory"]:
                return False
            _verify_projected(root, [name], basis["files"])
        return True
    except (OSError, WorkspaceError):
        return False


def publish(local, private, baseline, extensions, secret, *, return_basis=False, require_match=False,
            verify_paths=()):
    """Validate all edits before the first write. Replace through anchored directory handles."""
    with _directory(private) as fd:
        new = _scan(fd, extensions, secret)
    old = _baseline_files(baseline, extensions)
    proposed = {name: _digest(content) for name, content in new.items()}
    changes = sorted(k for k in old.keys() | proposed.keys() if old.get(k) != proposed.get(k))
    if not all(_project_path(name, extensions) for name in verify_paths):
        raise WorkspaceError("成员交接的文件路径无效。")
    with _directory(Path(local)) as root, ExitStack() as stack:
        if _identity(root) != baseline.get("directory"):
            raise WorkspaceError("所选文件夹已被移动或替换，本轮产物保存在聊天中。")
        if require_match:
            # A manual delivery can follow later runs or a user's own edits.
            # Allow an idempotent retry of this proposal, including a process
            # interruption between installing files and saving its receipt.
            current = {k: _digest(v) for k, v in _scan(root, extensions, secret).items()}
            for name in sorted(old.keys() | proposed.keys() | current.keys()):
                if current.get(name) not in (old.get(name), proposed.get(name)):
                    raise WorkspaceError(f"本机文件与该轮工作区版本不符，未覆盖：{name}。")
        parents = {(): root}
        created = []
        prepared = []
        applied = []
        try:
            _verify_projected(root, set(verify_paths).difference(changes), proposed)
            for name in changes:
                parts = Path(name).parts
                parent = root
                for depth, part in enumerate(parts[:-1], 1):
                    key = parts[:depth]
                    if key not in parents:
                        try:
                            child = os.open(part, _DIR, dir_fd=parent)
                        except FileNotFoundError:
                            os.mkdir(part, dir_fd=parent)
                            created.append((parent, part))
                            child = os.open(part, _DIR, dir_fd=parent)
                        parents[key] = child
                        stack.callback(os.close, child)
                    parent = parents[key]
                leaf = parts[-1]
                _check_database_idle(parent, leaf)
                previous = _contents(parent, leaf)
                _check_database_idle(parent, leaf)
                if return_basis and previous == new.get(name):
                    # Installation may already have finished before its journal
                    # was persisted; do not replace an identical destination.
                    continue
                if _digest(previous) != old.get(name):
                    raise WorkspaceError(f"本机文件已被修改，未覆盖：{name}。本轮产物保存在聊天中。")
                prepared.append((parent, leaf, previous, new.get(name)))
            for parent, leaf, before, after in prepared:
                mode = stat.S_IMODE(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode) if before is not None else 0o644
                _replace(parent, leaf, after, mode, expected=before)
                applied.append((parent, leaf, before, after, mode))
            _verify_projected(root, verify_paths, proposed)
        except BaseException:
            conflicts = []
            for parent, leaf, before, after, mode in reversed(applied):
                try:
                    _replace(parent, leaf, before, mode, expected=after)
                except (OSError, WorkspaceError):
                    conflicts.append(leaf)
            for parent, name in reversed(created):
                try:
                    os.rmdir(name, dir_fd=parent)
                except OSError:
                    pass
            if conflicts:
                raise WorkspaceError("部分文件无法回滚，请核对本地版本：" + "、".join(conflicts)) from None
            raise
    if return_basis:
        return changes, {"directory": baseline["directory"], "files": proposed}
    return changes


def _check_database_idle(parent, leaf):
    if is_database(leaf):
        for suffix in SQLITE_SIDECARS:
            try:
                os.stat(leaf + suffix, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise WorkspaceError(f"本机数据库正在使用：{leaf}。")


def _replace(parent, leaf, data, mode, *, expected):
    def verify():
        _check_database_idle(parent, leaf)
        if _contents(parent, leaf) != expected:
            raise WorkspaceError(f"发布期间本机文件已被修改：{leaf}。")

    if data is None:
        verify()
        os.unlink(leaf, dir_fd=parent)
        return
    name = ".studio-" + uuid.uuid4().hex
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            os.fchmod(fd, mode)
            stream.flush()
            os.fsync(fd)
        # Staging/fsync may take long enough for the user to save meanwhile.
        # A new destination also needs atomic no-clobber at installation.
        verify()
        if expected is None:
            try:
                os.link(name, leaf, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            except FileExistsError:
                raise WorkspaceError(f"发布期间本机文件已创建：{leaf}。") from None
        else:
            os.replace(name, leaf, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            pass
