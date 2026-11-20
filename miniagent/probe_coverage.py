"""Match a frozen failed Python assertion to a later observed case on the same product."""
from __future__ import annotations

import ast
import re


def _print_prefix(node):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "print" and node.args and not node.keywords):
        return ""
    value = node.args[0]
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Mod):
        value = value.left
    if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
        return ""
    # Only use a named test result actually present in stdout, never a bare PASS.
    prefix = value.value.split("%", 1)[0].strip()
    return prefix if re.match(r"^PASS\b\s*[:·-]?\s*\S", prefix, re.I) else ""


def _same_site(original, current):
    if ast.dump(original.test) != ast.dump(current.test):
        return False
    if isinstance(original.msg, ast.Constant) and isinstance(original.msg.value, str):
        return (isinstance(current.msg, ast.Constant) and isinstance(current.msg.value, str)
                and current.msg.value.startswith(original.msg.value))
    return (original.msg is None or current.msg is not None and ast.dump(original.msg) == ast.dump(current.msg))


def corrected_assertion(probe, code, output):
    """Locate the original traceback site and an observed, post-assert result in its new block.

    The result only identifies progress for another independent review. It is never
    an acceptance decision. Unrecognized tracebacks, conditional/missing output,
    changed assertion expressions, and ambiguous sites leave the obligation open.
    """
    if probe.get("tool") != "run_python" or not probe.get("exit_code"):
        return None
    error = probe.get("error", "").rpartition("Traceback (most recent call last):")[2]
    frames = re.findall(r'(?m)^\s*File "([^"\n]+)", line (\d+), in ([^\n]+)', error)
    if (not frames or frames[-1][0] != "<string>" or frames[-1][2] != "<module>"
            or not re.search(r"(?m)^AssertionError(?::.*)?\s*\Z", error)):
        return None
    line = int(frames[-1][1])
    try:
        original_tree = ast.parse(probe["code"])
        current_tree = ast.parse(code)
    except (ValueError, SyntaxError, RecursionError):
        return None
    sites = [n for n in ast.walk(original_tree) if isinstance(n, ast.Assert)
             and n.lineno <= line <= n.end_lineno]
    if len(sites) != 1:
        return None
    original = sites[0]
    # A second identical assertion in a different phase is not evidence for this
    # failed site unless the original diagnostic distinguishes it.
    if sum(isinstance(n, ast.Assert) and _same_site(original, n) for n in ast.walk(current_tree)) != 1:
        return None

    stdout = output.partition("stdout:\n")[2].partition("\nstderr:\n")[0].splitlines()
    prints = [_print_prefix(n) for n in ast.walk(current_tree) if isinstance(n, ast.Call)]
    blocks = [current_tree.body]
    while blocks:
        body = blocks.pop()
        matched = None
        for stmt in body:
            # Only a linear continuation or the body of a mandatory try/finally.
            # A conditional, nested function, caught error or jump breaks the chain.
            if isinstance(stmt, ast.Try) and not stmt.handlers:
                blocks.append(stmt.body)
            if isinstance(stmt, ast.Assert) and _same_site(original, stmt):
                matched = stmt
            elif matched and isinstance(stmt, ast.Expr) and (prefix := _print_prefix(stmt.value)):
                if (sum(p.startswith(prefix) for p in prints) == 1 and sum(s.startswith(prefix) for s in stdout) == 1):
                    return {"original_line": original.lineno, "checked_line": matched.lineno,
                            "observed_case": prefix[:160]}
                matched = None
            elif not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Assert, ast.Expr)):
                matched = None
    return None
