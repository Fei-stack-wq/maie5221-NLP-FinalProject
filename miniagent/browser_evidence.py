"""Execution evidence for one browser script. The parent can read progress even if the runner is killed."""
from __future__ import annotations

import ast
import json
from itertools import islice
import mmap
import re
import struct
import traceback


FILENAME = "<run_browser>"
MAX_DETAIL = 2400
INLINE_PENDING = 12


def clipped(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit * 2 // 3] + " … " + text[-limit // 3:]


class ScriptSteps:
    def __init__(self, code):
        self.lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.encoded = [line.encode("utf-8") for line in self.lines]
        self.name = "__studio_browser_evidence__"
        while self.name in code:
            self.name += "__"
        self.nodes = [None]  # zero means the script has not entered any statement
        self.ids = {}
        self.suites = {}
        self.parents = {}
        self.syntax_error = None
        try:
            self.tree = ast.parse(code, filename=FILENAME)
        except (SyntaxError, ValueError, RecursionError) as exc:
            self.syntax_error = exc
            return
        for parent in ast.walk(self.tree):
            for _, field in ast.iter_fields(parent):
                if isinstance(field, ast.AST):
                    self.parents[field] = parent
                elif isinstance(field, list):
                    suite = [node for node in field if isinstance(node, ast.stmt)]
                    for node in field:
                        if isinstance(node, ast.AST):
                            self.parents[node] = parent
                    for node in suite:
                        self.suites[node] = suite
                        self.ids[node] = len(self.nodes)
                        self.nodes.append(node)

    @property
    def progress_size(self):
        # Two statement IDs (last entered and current top-level statement), then one visited byte per ID.
        return 8 + len(self.nodes)

    def source(self, node):
        lines = self.encoded[node.lineno - 1:node.end_lineno]
        lines[-1] = lines[-1][:node.end_col_offset]
        lines[0] = lines[0][node.col_offset:]
        return b"\n".join(lines).decode("utf-8")

    def describe(self, node, *, include_body=False):
        if not include_body and any(isinstance(child, ast.stmt) for child in ast.iter_child_nodes(node)):
            source = self.lines[node.lineno - 1].strip()
        else:
            source = self.source(node)
        return {"line": node.lineno, "source": clipped(source, 600 if include_body else 240)}

    def at_frame(self, frame, last):
        candidates = [node for node in self.nodes[1:] if node.lineno <= frame.lineno <= node.end_lineno]
        column = getattr(frame, "colno", None)
        if column is not None:
            candidates = [n for n in candidates if (n.lineno, n.col_offset) <= (frame.lineno, column)
                          < (n.end_lineno, n.end_col_offset)]
        if not candidates:
            return None
        if last in candidates:
            return last
        return min(candidates, key=lambda n: (n.end_lineno - n.lineno, n.end_col_offset - n.col_offset))

    def report(self, progress, failure):
        last_id, top_id = struct.unpack_from("=II", progress)
        last = self.nodes[last_id] if last_id < len(self.nodes) else None
        top = self.nodes[top_id] if top_id < len(self.nodes) else None
        frames = [self.nodes[i] for i in failure.get("frames", []) if type(i) is int and 0 < i < len(self.nodes)]
        step = frames[-1] if frames else last
        pending = set()
        for node in [top, step, *frames]:
            while node in self.suites:
                suite = self.suites[node]
                for later in suite[suite.index(node) + 1:]:
                    if not progress[8 + self.ids[later]]:
                        pending.add(later)
                parent = self.parents.get(node)
                # A helper's definition is not the caller. Use the actual traceback for callers.
                while parent is not None and not isinstance(parent, ast.stmt):
                    parent = self.parents.get(parent)
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    break
                node = parent
        if step is None and not self.syntax_error:
            pending.update(self.tree.body)
        ordered = sorted(pending, key=lambda n: (n.lineno, n.col_offset))
        location = self.describe(step) if step else None
        if location is None and isinstance(self.syntax_error, SyntaxError) and self.syntax_error.lineno:
            location = {"line": self.syntax_error.lineno, "source": clipped((self.syntax_error.text or "").strip(), 240)}
        return {"kind": failure["kind"], "detail": failure["detail"], "observed": failure.get("observed", []),
                "step": location,
                "unreached": [self.describe(n, include_body=True) for n in ordered], "unreached_count": len(ordered)}

    def compile(self):
        if self.syntax_error:
            raise self.syntax_error
        tree = _Markers(self).visit(self.tree)
        return compile(ast.fix_missing_locations(tree), FILENAME, "exec")


class _Markers(ast.NodeTransformer):
    def __init__(self, steps):
        self.steps = steps

    def call(self, method, *args):
        return ast.Call(ast.Attribute(ast.Name(self.steps.name, ast.Load()), method, ast.Load()), list(args), [])

    def visit(self, node):
        if not isinstance(node, ast.stmt):
            return super().visit(node)
        # Preserve docstrings, future directives and compile-time declarations.
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                or isinstance(node, ast.ImportFrom) and node.module == "__future__"
                or isinstance(node, (ast.Global, ast.Nonlocal))):
            return node
        index = ast.Constant(self.steps.ids[node])
        if isinstance(node, ast.Assert):
            def observe(expr):
                label = clipped(self.steps.source(expr), 100)
                return ast.copy_location(self.call("observe", index, ast.Constant(label), expr), expr)
            if isinstance(node.test, ast.Compare):
                node.test.left = observe(node.test.left)
                node.test.comparators = [observe(expr) for expr in node.test.comparators]
            else:
                node.test = observe(node.test)
        marker = ast.Expr(self.call("enter", index, ast.Constant(self.steps.parents[node] is self.steps.tree)))
        return [ast.copy_location(marker, node), super().visit(node)]


def _operand(value, depth=0):
    """Render ordinary scalar and collection results without calling user-defined code."""
    kind = type(value)
    if value is None or kind is bool or kind is float or (kind is int and value.bit_length() < 4096):
        return clipped(repr(value), 260)
    if kind is str:
        return clipped(repr(clipped(value, 240)), 260)
    if depth >= 2 or (kind is not list and kind is not tuple and kind is not dict):
        return None
    parts = []
    # Iterating items avoids invoking a custom key's __hash__ or __eq__.
    for item in islice(value.items() if kind is dict else value, 8):
        if kind is dict:
            key, item = item
            key_text = _operand(key, depth + 1)
            if key_text is None:
                return None
        text = _operand(item, depth + 1)
        if text is None:
            return None
        parts.append(key_text + ": " + text if kind is dict else text)
    if len(value) > 8:
        parts.append("…")
    left, right = {list: ("[", "]"), tuple: ("(", ")"), dict: ("{", "}")}[kind]
    body = ", ".join(parts) + ("," if kind is tuple and len(value) == 1 else "")
    return clipped(left + body + right, 260)


class Recorder:
    def __init__(self, steps, file):
        self.steps = steps
        self.file = file
        self.progress = mmap.mmap(file.fileno(), steps.progress_size)
        self.observations = {}

    def enter(self, index, top_level):
        struct.pack_into("=I", self.progress, 0, index)
        if top_level:
            struct.pack_into("=I", self.progress, 4, index)
        self.progress[8 + index] = 1
        self.observations.pop(index, None)

    def observe(self, index, label, value):
        # Return the very same operand. Never replay an expression or invoke application __repr__.
        rendered = _operand(value)
        if rendered is not None:
            values = self.observations.setdefault(index, [])
            if len(values) < 8:
                values.append(f"{label} = {rendered}")
        return value

    def failure(self, exc, kind):
        last = self.steps.nodes[struct.unpack_from("=I", self.progress)[0]]
        frames = []
        for frame in traceback.extract_tb(exc.__traceback__):
            if frame.filename == FILENAME:
                node = self.steps.at_frame(frame, last)
                if node is not None:
                    frames.append(self.steps.ids[node])
        detail = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
        observations = self.observations.get(frames[-1], []) if frames else []
        if kind == "assertion_failed" and not observations:
            # Preserve values actually supplied by Playwright. Never poll the page again for a diagnosis.
            observations = [clipped(line, 300) for line in detail.splitlines() if line.startswith("Actual value:")][:1]
        data = {"kind": kind, "detail": clipped(f"{type(exc).__name__}: {detail}", MAX_DETAIL),
                "frames": list(reversed(dict.fromkeys(reversed(frames)))), "observed": observations}
        self.file.seek(self.steps.progress_size)
        self.file.write(json.dumps(data, ensure_ascii=True).encode())
        self.file.flush()


def format_report(report):
    step = report.get("step")
    status = f" (exit_code={report['exit_code']})" if report.get("exit_code") is not None else ""
    parts = [f"Browser check: {report['kind']}{status}"]
    if step:
        where = "Last entered step" if report["kind"] in ("script_timeout", "interrupted") else "Script step"
        parts.append(f"{where} L{step['line']}: {step['source']}")
    parts.append(report['detail'])
    parts.extend("Observed: " + value for value in report.get("observed", []))
    for item in report["unreached"][:INLINE_PENDING]:
        parts.append(f"Not reached L{item['line']}: {item['source']}")
    extra = report["unreached_count"] - min(INLINE_PENDING, len(report["unreached"]))
    if extra:
        parts.append(f"{extra} more unreached script statements in the tool trace.")
    return "\n".join(parts)
