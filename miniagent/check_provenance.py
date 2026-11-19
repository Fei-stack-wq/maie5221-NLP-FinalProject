"""Identify a runner independently of what its stdout claims."""
from __future__ import annotations

import ast
import hashlib
import re
import shlex
import sys
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class Invocation:
    scope: str
    selection: str
    caption: str
    script: str
    complete: bool
    exit_footer: bool = False
    legacy_default_scope: str = ""


def _invocation(argv, cwd, complete):
    if not argv or any(not re.fullmatch(r"[\w./:=+@-]+", arg) for arg in argv):
        return None
    name, *args = argv
    name = Path(name).name
    script = ""
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", name) or name == "node":
        if args[:1] == ["-u"] and name.startswith("python"):
            args = args[1:]
        if args[:2] in (["-m", "pytest"], ["-m", "unittest"]) or args[:1] == ["--test"] and name == "node":
            pass
        elif args and args[0].endswith((".py", ".js", ".mjs", ".cjs")):
            path = Path(cwd) / args[0]
            if path.is_absolute() or ".." in path.parts:
                return None
            script = str(path)
            args = [str(Path(args[0])), *args[1:]]
        else:
            return None
    elif name not in {"pytest", "unittest", "jest", "vitest"}:
        return None
    # Never expose arbitrary argument values/fetch URLs to a peer. The digest
    # still distinguishes interpreters, paths, flags, and selected test cases.
    key = "\0".join((str(Path(cwd)), argv[0], *args))
    if script and len(args) == 1:
        caption = shlex.join([argv[0], args[0]]) + (" (cwd=" + str(Path(cwd)) + ")" if Path(cwd) != Path(".") else "")
    else:
        caption = script or name + (" " + args[1] if args[:1] == ["-m"] else "")
    selection = "\0".join((str(Path(cwd)), *args))
    legacy_scope = ""
    if argv[0] == "python":
        # Older native wrappers recorded the platform's absolute interpreter.
        # Match only that exact executable, directory, flags and case selection.
        old_key = "\0".join((str(Path(cwd)), sys.executable, *args))
        legacy_scope = hashlib.sha256(old_key.encode()).hexdigest()
    return Invocation(hashlib.sha256(key.encode()).hexdigest(), hashlib.sha256(selection.encode()).hexdigest(),
                      caption[:160], script, complete, legacy_default_scope=legacy_scope)


def shell_invocation(command, cwd):
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        words = list(lex)
    except ValueError:
        return None
    # Only the leading executable can supply a runner identity. Everything
    # following shell control syntax is non-authoritative except a direct
    # stderr merge or an echo of this exact process's exit code.
    end = next((i for i, w in enumerate(words) if w in {";", "|", "&", "&&", "||", ">", ">&", "<"}), len(words))
    argv, tail = words[:end], words[end:]
    if argv[-1:] == ["2"] and tail[:2] == [">&", "1"]:
        argv, tail = argv[:-1], tail[2:]
    complete = not tail or tail == [";", "echo", "EXIT=$?"]
    invocation = _invocation(argv, cwd, complete and "\n" not in command.strip())
    if invocation and invocation.complete and tail:
        return replace(invocation, exit_footer=True)
    return invocation


def python_invocation(code, cwd):
    try:
        tree = ast.parse(code)
    except (ValueError, SyntaxError, RecursionError):
        return None
    launches = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"
                and n.func.attr in {"run", "Popen", "call", "check_call", "check_output"} and n.args]
    if len(launches) != 1 or not isinstance(launches[0].args[0], (ast.Tuple, ast.List)):
        return None
    launch = launches[0]
    argv = []
    for arg in launch.args[0].elts:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            argv.append(arg.value)
        elif isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name) and arg.value.id == "sys" and arg.attr == "executable":
            # Each member has its own absolute interpreter path. Record the
            # replayable shell name for the same default member interpreter.
            argv.append("python")
        else:
            return None
    # A snippet that analyzes, synthesizes, reads back, or filters output has
    # a diagnostic transcript. Only direct and untouched forwarding is usable.
    result = None
    complete = launch.func.attr != "Popen" and all(
        k.arg in {"capture_output", "text", "stdout", "stderr", "check"} and (
            isinstance(k.value, ast.Constant) and (k.value.value is None or type(k.value.value) == bool)
            or isinstance(k.value, ast.Attribute) and isinstance(k.value.value, ast.Name)
            and k.value.value.id == "subprocess" and k.value.attr in {"PIPE", "STDOUT", "DEVNULL"})
        for k in launch.keywords)
    for stmt in tree.body:
        if isinstance(stmt, ast.Import) and all(name.name in {"subprocess", "sys"} and not name.asname
                                                for name in stmt.names):
            continue
        if isinstance(stmt, ast.Assign) and stmt.value is launch and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            result = stmt.targets[0].id
            continue
        expr = stmt.value if isinstance(stmt, ast.Expr) else None
        if expr is launch:
            continue
        if isinstance(stmt, ast.Raise) and isinstance(stmt.exc, ast.Call) and isinstance(stmt.exc.func, ast.Name) and stmt.exc.func.id == "SystemExit":
            args = stmt.exc.args
            if (len(args) == 1 and isinstance(args[0], ast.Attribute) and args[0].attr == "returncode"
                    and isinstance(args[0].value, ast.Name) and args[0].value.id == result):
                continue
        if (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "print"
                and result and not expr.keywords and expr.args and all(
                    isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id == result
                    and a.attr in {"stdout", "stderr", "returncode"} or isinstance(a, ast.Constant)
                    and type(a.value) == int for a in expr.args)):
            continue
        complete = False
    return _invocation(argv, cwd, complete)


def python_check_source(data: bytes) -> bool:
    """Recognize assertions or an exercised conditional custom reporter, not printed claims."""
    if b"assert" not in data and (b"FAIL" not in data or b"PASS" not in data):
        return False
    try:
        tree = ast.parse(data)
    except (SyntaxError, ValueError, RecursionError):
        return False
    nodes = list(ast.walk(tree))
    if any(isinstance(n, ast.Assert) or isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr.startswith("assert") for n in nodes):
        return True
    for node in (n for n in nodes if isinstance(n, ast.FunctionDef)):
        body = set(ast.walk(node))
        outcomes = [n.value for n in body if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        if (any("PASS" in v for v in outcomes) and any("FAIL" in v for v in outcomes)
                and any(isinstance(n, (ast.If, ast.IfExp)) for n in body)
                and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == node.name
                        for n in nodes if n not in body)):
            return True
    return False
