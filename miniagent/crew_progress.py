"""Runtime evidence that decides when a Studio member should hand work back.

Check output guides turn scheduling. The configured acceptance checker still
owns delivery verification, and publishing still validates the actual files.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .check_provenance import python_check_source, python_invocation, shell_invocation
from .project_data import PROJECT_CACHE_DIRS
from .rao import Budget, Observation, RAOState, StepRecord


_UNIT = re.compile(r"\bRan (\d{1,8}) tests? in [\d.]+s\b")
_UNIT_OK = re.compile(r"^OK(?: \([^\n]*\))?\s*$", re.M)
_PYTEST = re.compile(r"(?m)^\s*=*\s*(?:\d+ (?:skipped|warnings?), )*(\d{1,8}) passed\b[^\n]*$")
_FAILURE = re.compile(r"(?m)^\s*(?:=+\s*)?(?:FAILED\s*\(|(?:\d+ (?:passed|skipped|warnings?), )*"
                      r"[1-9]\d* (?:failed|errors?)\b|FAILED \S+::)")
_CASE_DETAIL = re.compile(r"\s{2,}|\s+->\s+")
_CASE = re.compile(r"(?im)^\s*(PASS|FAIL)\s*[:|–-]?\s+([^\n]+)")
# Custom test scripts may append a balanced tally, then key-value diagnostics.
# Unlike a result event, an empty failure inventory has no test identity.
_CASE_TOTALS = re.compile(
    r"[ \t=*-]*(?:[^:\n：]{1,40}[:：][ \t]*)?PASS[ \t]+(\d{1,8})[ \t]*/[ \t]*(\d{1,8})"
    r"[ \t]*[,，][ \t]*FAIL[ \t]+(\d{1,8})[ \t=*-]*", re.I)
_EMPTY_FAILURE_DETAILS = re.compile(
    r"(?:details?|list|failed (?:cases?|tests?)|明细|详情|列表|失败(?:明细|列表|用例))"
    r"\s*[:：]\s*(?:none|null|\[\s*\]|\{\s*\}|无|空)", re.I)
_NESTED_EXIT = re.compile(r"(?im)(?:^\s*|CompletedProcess\([^\n]*?,\s*)(?:returncode|exit(?:[_ ]?code)?)\s*[=:]\s*(-?\d{1,8})\b")
_TRACEBACK = re.compile(r"(?ms)^Traceback \(most recent call last\):\s*\n\s+File .+?"
                        r"^(?:[\w.]+(?:Error|Exception)|SystemExit)(?::|$)")
_MUTATORS = {"search_sources", "http_get", "run_python", "run_shell", "poll_process", "run_browser", "write_file", "edit_file", "append_file", "delete_file"}
_READER = {"open", "read_text", "read_bytes"}
_SCAN_FILES = 1000
_SCAN_BYTES = 20 * 1024 * 1024


def _tool_args(state: RAOState) -> dict:
    for message in reversed(state.history):
        if message.get("role") == "assistant":
            args = {}
            for call in message.get("tool_calls", []):
                try:
                    parameters = json.loads(call["function"]["arguments"])
                    if isinstance(parameters, dict):
                        args[call["id"]] = parameters
                except (KeyError, ValueError, TypeError):
                    continue
            return args
    return {}


def _script_inputs(code: str) -> tuple[set[str], str, int | None, bool]:
    """Identify file reads and the test runner, independently of print formatting."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        return set(), "", None, False
    paths = set()
    reader_seen = False
    calls = []
    command = ""
    exit_position = None
    literals = {node.targets[0].id: node.value for node in tree.body if isinstance(node, ast.Assign)
                and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        calls.append(name)
        if name == "print":
            for index, arg in enumerate(node.args):
                if isinstance(arg, ast.Attribute) and arg.attr == "returncode":
                    exit_position = index
        if name in _READER:
            reader_seen = True
            arg = node.args[0] if node.args else None
            if name != "open" and isinstance(node.func, ast.Attribute):
                receiver = node.func.value
                arg = receiver.args[0] if isinstance(receiver, ast.Call) and receiver.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                paths.add(arg.value)
        if name not in {"run", "Popen", "call", "check_call", "check_output", "system", "main"}:
            continue
        if not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Name):
            arg = literals.get(arg.id, arg)
        tokens = [n.value for n in ast.walk(arg) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        if tokens:
            command = " ".join(tokens)[:500]
    if not command:
        assertions = [ast.dump(node.test) for node in tree.body if isinstance(node, ast.Assert)]
        if assertions:
            digest = hashlib.sha256("\n".join(assertions).encode()).hexdigest()[:16]
            command = "assert " + digest
    output_only = (bool(calls) and all(name in {"print", "format", "join", "str", "repr"} for name in calls))
    return paths, command, exit_position, reader_seen or output_only


def _shell_inputs(command: str, cwd: str, files: dict) -> tuple[set, str, bool]:
    """Recognise navigation and printed source separately from a real checker invocation."""
    try:
        words = shlex.split(command)
    except ValueError:
        return set(), command, False
    if not words:
        return set(), "", True
    executable = Path(words[0]).name
    base = Path(cwd or ".")
    section = 0
    if executable == "sed":
        for word in words:
            match = re.fullmatch(r"(\d+)(?:,\d+)?p", word)
            if match:
                section = int(match[1])
                break
    reads = {(str(base / word), section) for word in words if str(base / word) in files}
    inspection = executable in {"cat", "head", "tail", "less", "more", "sed", "nl", "ls", "find",
                                "rg", "grep", "pwd", "echo", "printf"}
    if "-c" in words and executable.startswith("python"):
        index = words.index("-c")
        if index + 1 < len(words):
            _, nested, _, only_read = _script_inputs(words[index+1])
            if only_read and not nested:
                inspection = True
    scope = shlex.join(words)
    return reads if inspection else set(), (f"(cwd={base}) " if base != Path(".") else "") + scope, inspection


def _shell_case_suite(command: str, cwd: str) -> str | None:
    """Attribute custom PASS/FAIL lines only to a single identifiable runner.

    Allow ordinary stdout formatting and viewing a log produced by that runner
    in this invocation. Any other producer, unrelated file, expansion, or mixed
    test suite falls back to the exact original invocation.
    """
    if any(c in command.strip() for c in ("\n", "`", "$(", "<<")):
        return None
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError:
        return None
    suite = None
    logs = set()
    piped = False
    words = []
    for token in [*tokens, ";"]:
        if token not in ("|", ";"):
            words.append(token)
            continue
        if not words:
            return None
        # The sole writer of named test-result lines must be the same runner.
        name, *args = words
        if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|node", Path(name).name) and args and not piped:
            redirect = next((j for j, arg in enumerate(args) if arg in (">", ">&")), len(args))
            argv = args[:redirect]
            tail = args[redirect:]
            # shlex tokenizes '2>&1' into ['2', '>&', '1'].
            if tail[:2] == [">&", "1"] and argv[-1:] == ["2"]:
                argv.pop()
                tail = tail[2:]
            if tail[:1] == [">"] and len(tail) >= 2 and re.fullmatch(r"[\w./-]+", tail[1]):
                logs.add(tail[1])
                tail = tail[2:]
            if tail not in ([], ["2", ">&", "1"]):
                return None
            if (not argv or not argv[0].endswith((".py", ".js", ".mjs", ".cjs"))
                    or any(not re.fullmatch(r"[\w./:=+@-]+", arg) for arg in [name, *argv])):
                return None
            identity = shlex.join([name, *argv])
            if suite is not None and suite != identity:
                return None
            suite = identity
        elif name in ("sed", "grep", "head", "tail") and suite:
            if not piped and (not args or args.pop() not in logs):
                return None
            if name == "sed":
                if args[:1] == ["-e"]:
                    args.pop(0)
                if args != [r"s/\x1b\[[0-9;]*m//g"] and not (
                        len(args) == 2 and args[0] == "-n" and re.fullmatch(r"\d+(?:,\d+)?p", args[1])):
                    return None
            elif name == "grep":
                if args and re.fullmatch(r"-[EFiv]+", args[0]):
                    args.pop(0)
                if len(args) != 1 or args[0].startswith("-"):
                    return None
            elif not (len(args) == 1 and re.fullmatch(r"-\d+", args[0]) or
                      len(args) == 2 and args[0] == "-n" and args[1].isdigit()):
                return None
        elif not piped and name == "echo" and len(args) == 1 and re.fullmatch(
                r"-+|(?:EXIT|EXIT_CODE|returncode)=\$(?:\?|RC|rc)", args[0], re.I):
            pass
        elif not piped and name in ("RC=$?", "rc=$?") and not args:
            pass
        else:
            return None
        words = []
        piped = token == "|"
    return f"shell case {Path(cwd or '.')} {suite}" if suite else None



class _ScriptLayout(NamedTuple):
    shape: str
    captions: tuple[str, ...]


class _AssertionShape(ast.NodeTransformer):
    """Permit revised literal samples while retaining operators and real operands."""
    def visit_Constant(self, node):
        if type(node.value) in (int, float, str, bytes):
            return ast.Constant("<" + type(node.value).__name__ + ">")
        return node

    def visit_Subscript(self, node):
        # Changing which property supplies the observation needs a new review.
        return node


def _script_layout(source: str) -> _ScriptLayout | None:
    """Fingerprint reporting and assertion coverage for an eligible QA handoff.

    Fixtures may change. This compares neither business semantics nor the rest
    of the program; only independent acceptance can approve that change.
    """
    try:
        tree = ast.parse(source)
        functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        reporters = set()
        for name, function in functions.items():
            terms = " ".join(n.value for n in ast.walk(function)
                             if isinstance(n, ast.Constant) and isinstance(n.value, str)).upper()
            if "PASS" in terms and "FAIL" in terms:
                reporters.add(name)
        if not reporters:
            return None
        # Recognize wrappers forwarding their caller's caption to the reporter.
        # Exact function bodies must survive, including their comparisons.
        while True:
            wrappers = {name for name, function in functions.items() if name not in reporters and any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in reporters
                and n.args and isinstance(n.args[0], ast.Name)
                and n.args[0].id in {a.arg for a in function.args.args} for n in ast.walk(function))}
            if not wrappers:
                break
            reporters.update(wrappers)
        protocol = [ast.dump(functions[name]) for name in sorted(reporters)]
        internal = {n for name in reporters for n in ast.walk(functions[name])}
        nodes = sorted((n for n in ast.walk(tree) if isinstance(n, (ast.Call, ast.Assert, ast.Return, ast.Raise))
                        and n not in internal), key=lambda n: (n.lineno, n.col_offset))
        captions = []
        for node in nodes:
            if isinstance(node, ast.Call):
                func = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if func in reporters and isinstance(node.func, ast.Name) and len(node.args) >= 2:
                    caption = node.args[0]
                    if isinstance(caption, ast.Constant) and isinstance(caption.value, str):
                        captions.append(caption.value)
                    protocol.append(ast.dump(_AssertionShape().visit(node)))
                elif func in {"print", "exit", "_exit", "quit", "write"}:
                    protocol.append(ast.dump(node))
            elif isinstance(node, ast.Assert):
                protocol.append(ast.dump(_AssertionShape().visit(node)))
            else:
                protocol.append(ast.dump(node))
        if not captions or len(set(captions)) != len(captions):
            return None
        shape = hashlib.sha256("\n".join(protocol).encode()).hexdigest()
        return _ScriptLayout(shape, tuple(captions))
    except (ValueError, SyntaxError, RecursionError):
        return None


class _CaseReport(NamedTuple):
    rows: list[tuple[str, str]]
    reported_failure: bool
    incomplete: bool


def _case_report(output: str) -> _CaseReport:
    rows = []
    reported_failure = incomplete = False
    after_complete_tally = False
    for line in output.splitlines():
        totals = _CASE_TOTALS.fullmatch(line)
        if totals:
            passed, total, failed = map(int, totals.groups())
            reported_failure |= failed > 0
            incomplete |= passed + failed != total or total == 0
            after_complete_tally = passed == total and total > 0 and failed == 0
            continue
        case = _CASE.fullmatch(line)
        if case:
            status, name = case[1].upper(), case[2].strip()
            if after_complete_tally and status == "FAIL" and _EMPTY_FAILURE_DETAILS.fullmatch(name):
                continue
            rows.append((status, name))
            after_complete_tally = False
        elif after_complete_tally and line.strip() and not line.strip().isdigit():
            after_complete_tally = False
    return _CaseReport(rows, reported_failure, incomplete)


class _ScriptRun(NamedTuple):
    path: str
    digest: str
    layout: _ScriptLayout
    # One entry per result, in actual emission order. Static captions are
    # represented by their source index, dynamic passing captions by exact text.
    order: tuple
    failed: dict
    exit_code: int


def _script_run(layouts, current, command, cwd, obs, status):
    # Only a complete, unfiltered local script invocation certifies a revision.
    # An echo wrapper must directly observe the runner's exit and print it last.
    match = re.fullmatch(r'\s*python(?:\d+(?:\.\d+)*)?\s+([\w./-]+\.py)'
                         r'(?:\s*;\s*echo\s+(?:"EXIT=\$\?"|EXIT=\$\?))?\s*', command)
    if not match or _output_truncated(obs) or obs.meta.get("timed_out"):
        return None
    path = Path(cwd) / match[1]
    if path.is_absolute() or ".." in path.parts or str(path) not in current:
        return None
    digest = current[str(path)]
    exit_code = obs.meta.get("exit_code")
    if ";" in command:
        last = re.search(r"(?m)^EXIT=(-?\d+)\s*\Z", obs.content)
        if exit_code != 0 or not last:
            return None
        exit_code = int(last[1])
    if type(exit_code) != int or ((status == "passed") != (exit_code == 0)):
        return None
    version, layout = layouts.get(str(path), (None, None))
    if not layout or version != digest:
        return None
    order, failed = [], {}
    report = _case_report(re.sub(r"\x1b\[[0-9;]*m", "", obs.content))
    if report.incomplete:
        return None
    for result, name in report.rows:
        name = name.strip()
        # Match a passing row's literal source caption independently of its
        # changing diagnostics. A shared word prefix is not a case identity.
        matches = [i for i, cap in enumerate(layout.captions)
                   if name == cap or name.startswith(cap + " ") and
                   (result == "FAIL" or _CASE_DETAIL.match(name, len(cap)))]
        if len(matches) == 1:
            key = matches[0]
        elif result.upper() == "PASS" and not matches:
            key = name
        else:
            return None
        order.append(key)
        if result.upper() == "FAIL":
            failed[name] = key
    if not order or len(set(order)) != len(order):
        return None
    return _ScriptRun(str(path), digest, layout, tuple(order), failed, exit_code)


class _Check(NamedTuple):
    suite: str
    status: str
    label: str
    cases: tuple[str, ...] = ()


def _browser_check(obs: Observation, code: str, cwd: str) -> _Check | None:
    """A browser may print arbitrary site text. Only use script shape and host exit."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        return None
    # The browser harness injects an actual 'expect'. Statements inside an
    # uncalled helper or conditional are not an observed test run.
    has_expectation = False
    for stmt in tree.body:
        if isinstance(stmt, ast.Assert):
            has_expectation = True
            break
        expr = stmt.value if isinstance(stmt, ast.Expr) else None
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
            method = expr.func
            if (method.attr.startswith(("to_", "not_to_")) and isinstance(method.value, ast.Call)
                    and isinstance(method.value.func, ast.Name) and method.value.func.id == "expect"):
                has_expectation = True
                break
    if not has_expectation:
        return None
    # Include setup, route, selectors and working directory. An unrelated
    # test with the same final 'assert value' must not clear a prior failure.
    suite = hashlib.sha256((str(Path(cwd)) + "\0" + ast.dump(tree)).encode()).hexdigest()
    identity = "browser " + suite
    if not obs.ok or obs.meta.get("timed_out") or obs.meta.get("exit_code") != 0:
        return _Check(identity, "failed", "浏览器断言脚本未正常退出")
    return _Check(identity, "passed", "含浏览器断言的脚本退出 0")


def _check_key(suite: str, case: str = "") -> str:
    # Custom runners often print a stable case caption, then two spaces
    # and the actual result. Keep the expected assertion in the caption.
    caption = _CASE_DETAIL.split(case.strip(), maxsplit=1)[0]
    return hashlib.sha256((suite + "\0" + caption).encode()).hexdigest()


def _output_truncated(obs: Observation) -> bool:
    # Legacy run_python embedded an exact adapter-generated marker. A case
    # title or fixture containing the word "truncated" is ordinary output.
    return bool(obs.truncated or obs.meta.get("truncated") or
                re.search(r"(?m)^\.\.\.\[已截断，原文 [0-9]+ 字符\]\s*\Z", obs.content))


def _check(obs: Observation, command: str, reading: bool, exit_position: int | None) -> _Check | None:
    # Ignore lines that merely render source, e.g. print(Path(...).read_text()).
    # A known check command which reads input fixtures may still emit a result.
    is_test = bool(re.search(r"(?i)\b(?:pytest|unittest|jest|vitest)\b|(?:^|[/\s_.-])tests?(?:$|[/\s_.-])", command))
    inline_assertions = command.startswith("assert ")
    if reading and not (is_test or inline_assertions):
        return None
    output = obs.content.partition("\n")[2] if obs.content.startswith("exit_code=") else obs.content
    output = re.sub(r"\x1b\[[0-9;]*m", "", output)
    # Common wrapper: print(result.returncode, result.stderr.splitlines()[-2:]).
    # Parse a literal list of lines; do not interpret arbitrary Python output.
    for line in output.splitlines()[-4:]:
        pos = line.find("[")
        if pos >= 0 and line.rstrip().endswith("]"):
            try:
                lines = ast.literal_eval(line[pos:])
                if isinstance(lines, list) and all(isinstance(s, str) for s in lines):
                    output += "\n" + "\n".join(lines)
            except (ValueError, SyntaxError, RecursionError):
                pass
    unit = _UNIT.search(output)
    passed = _PYTEST.search(output)
    report = _case_report(output)
    cases = report.rows
    failed_cases = [name.strip() for status, name in cases if status.upper() == "FAIL"]
    passed_cases = [name.strip() for status, name in cases if status.upper() == "PASS"]
    failure = _FAILURE.search(output)
    nested = [int(s) for s in _NESTED_EXIT.findall(output)]
    # Some callers print numeric exit codes without a label. Only interpret
    # the slot that the script actually filled from CompletedProcess.returncode.
    if exit_position is not None:
        integer_rows = [line.split() for line in output.splitlines() if
                        re.fullmatch(r"-?\d{1,8}(?:\s+-?\d{1,8})*", line.strip())]
        if len(integer_rows) == 1 and len(integer_rows[0]) > exit_position:
            nested.append(int(integer_rows[0][exit_position]))
    if unit or "unittest" in command:
        framework = "unittest"
    elif passed or "pytest" in command:
        framework = "pytest"
    elif inline_assertions:
        framework = "Python 断言"
    else:
        framework = "check"
    # Use runner arguments when available. A pass from an unrelated suite
    # cannot clear a previous failing one. Ignore verbosity-only options.
    suite = re.sub(r"(?:^|\s)-(?:q+|v+)(?=\s|$)", "", command).strip() or framework
    check_context = is_test or inline_assertions or unit or passed or nested or cases
    bad_exit = not obs.ok or obs.meta.get("exit_code", 0) != 0 or any(n != 0 for n in nested)
    nested_exception = (is_test or inline_assertions) and _TRACEBACK.search(output) and not unit
    if failed_cases:
        return _Check(suite, "failed", "工具输出 FAIL · " + failed_cases[0][:72], tuple(failed_cases))
    if failure or report.reported_failure or nested_exception or (check_context and bad_exit):
        return _Check(suite, "failed", f"{framework} 执行失败")
    if report.incomplete or _output_truncated(obs) or not obs.ok:
        return None
    if unit and int(unit[1]) and _UNIT_OK.search(output[unit.end():]):
        return _Check(suite, "passed", f"unittest · {unit[1]} tests OK", tuple(passed_cases))
    if passed and int(passed[1]):
        return _Check(suite, "passed", f"pytest · {passed[1]} passed", tuple(passed_cases))
    if passed_cases:
        return _Check(suite, "passed", f"工具输出 {len(passed_cases)} 项 PASS · " + " / ".join(name[:72] for name in passed_cases[:2]), tuple(passed_cases))
    if inline_assertions and obs.meta.get("exit_code") == 0:
        # A silent script with top-level assertions may have exited early.
        # It can clear its own failed invocation, not certify a suite pass.
        return _Check(suite, "clean_exit", "")
    return None


def _cover(ranges: list[tuple[int, int]], start: int, end: int) -> bool:
    """Merge half-open ranges and return whether the tool returned new source."""
    if end <= start or any(left <= start and end <= right for left, right in ranges):
        return False
    merged = []
    for left, right in sorted([*ranges, (start, end)]):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    ranges[:] = merged
    return True


@dataclass
class _ReadCoverage:
    sha256: str
    chars: int
    ranges: list[tuple[int, int]] = field(default_factory=list)
    lines: list[tuple[int, int]] = field(default_factory=list)
    cursor: dict = field(default_factory=dict)

    @property
    def complete(self):
        return not self.chars or self.ranges == [(0, self.chars)]

    def receipt(self) -> dict:
        # Character offsets are half-open like read_file; numbered lines are inclusive.
        return {**self.cursor, "sha256": self.sha256, "chars": self.chars, "complete": self.complete,
                "char_ranges": list(self.ranges[:8]), "line_ranges": [(a, b - 1) for a, b in self.lines[:8]],
                "range_list_truncated": len(self.ranges) > 8 or len(self.lines) > 8}


class MemberProgress:
    def __init__(self, workspace: Path, required_paths=(), *, public_baseline: dict[str, str] | None = None):
        self.workspace = Path(workspace)
        self.required = set(required_paths)
        self._cache = {}
        self._assertive_scripts = {}
        self._authored = set()
        self._layouts = {}
        self._restored_keys = set()
        self._failed_runs = {}
        self._retired = []
        self._active_retirements = {}
        self._current = self._scan()
        self._starting = self._current.copy()
        # A recovered member starts from its private checkpoint, while work
        # credited to the assignment is always relative to the public project.
        self._initial = self._current.copy() if public_baseline is None else dict(public_baseline)
        self._reads = set()
        self._read_coverage: dict[str, _ReadCoverage] = {}
        self._read_hint = ""
        self._inspected = set()
        self._passes = {}
        self._review_evidence = {}
        self._inline_failures = {}
        self._diagnostic_checks = {}
        self._pending_verification = {}
        self._complete_verification = {}
        self._script_exits = {}
        self._failures = {}
        self._prior_passes = []
        self._executions = self._shell_calls = self._browser_calls = self._clean_exits = 0
        self._navigation = set()
        self._searches = set()
        self._quiet = 0
        self._resumed = False
        self._investigations = set()
        self._after_pass = 0
        self._repeat_pass = False
        self._steps = 0
        self._stop = ""
        self._rejection = ""

    def _scan(self) -> dict[str, str]:
        """Cache content hashes; repeated test runs often rewrite identical bytes."""
        files = {}
        spent = 0
        entries = 0
        for directory, dirs, names, fd in os.fwalk(self.workspace, follow_symlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in PROJECT_CACHE_DIRS)
            entries += len(dirs) + len(names)
            if entries > _SCAN_FILES:
                break
            for name in sorted(names):
                if name.startswith(".") or name.endswith((".pyc", "-wal", "-shm", "-journal")):
                    continue
                key = str((Path(directory) / name).relative_to(self.workspace))
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        continue
                    sig = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    previous = self._cache.get(key)
                    if previous and previous[0] == sig:
                        files[key] = previous[1]
                        continue
                    if info.st_size > min(8 * 1024 * 1024, _SCAN_BYTES - spent):
                        continue
                    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    with os.fdopen(handle, "rb") as stream:
                        opened = os.fstat(handle)
                        if not os.path.samestat(info, opened) or not stat.S_ISREG(opened.st_mode):
                            continue
                        data = stream.read(info.st_size + 1)
                    if len(data) != info.st_size:
                        continue
                    spent += len(data)
                    files[key] = hashlib.sha256(data).hexdigest()
                    self._cache[key] = (sig, files[key])
                    if key.endswith(".py") and len(data) <= 256 * 1024 and python_check_source(data):
                        self._assertive_scripts[key] = files[key]
                    if key in self._authored and len(data) <= 256 * 1024:
                        try:
                            layout = _script_layout(data.decode("utf-8"))
                        except UnicodeError:
                            layout = None
                        self._layouts[key] = (files[key], layout)
                except OSError:
                    continue
        return files

    def review_evidence(self):
        """Current actual check outcomes, keyed by scope and linked to their tool invocation."""
        active = {"pass:" + _check_key(suite) for suite in self._passes} | {"fail:" + key for key in self._failures}
        return {ref:row for ref,row in self._review_evidence.items() if ref in active}

    def inline_failures(self):
        return {k:v for k,v in self._inline_failures.items() if k in self._failures}

    def project_versions(self):
        """Recheck candidate bytes using the same no-follow scan as member scheduling."""
        return self._scan()

    def observe(self, record: StepRecord, state: RAOState) -> None:
        self._steps += 1
        if not record.observations:
            return
        self._read_hint = ""
        args = _tool_args(state)
        before = self._current
        for obs in record.observations:
            if obs.ok and obs.source == "write_file":
                path = obs.meta.get("path") or args.get(obs.tool_call_id, {}).get("path")
                if isinstance(path, str):
                    path = str(Path(path))
                    if path.endswith(".py") and path not in before and path not in self._initial:
                        self._authored.add(path)
        changed = False
        if any(o.source in _MUTATORS for o in record.observations):
            files = self._scan()
            changed = files != self._current
            self._current = files
        if changed:
            for key, entry in list(self._active_retirements.items()):
                if self._current.get(entry["path"]) != entry["current_sha256"]:
                    self._failures[key] = entry["prior_failure"]
                    entry["invalidated_at_step"] = self._steps
                    del self._active_retirements[key]
            self._prior_passes = list(dict.fromkeys([*self._prior_passes, *self._passes.values()]))[-4:]
            self._passes.clear()
            self._complete_verification.clear()
            self._script_exits.clear()
            self._repeat_pass = False
            self._after_pass = 0
            self._reads.clear()
            self._investigations.clear()
            for path, coverage in list(self._read_coverage.items()):
                if self._current.get(path) != coverage.sha256:
                    del self._read_coverage[path]
        seen_before = bool(self._passes)
        advances = changed
        last_mutation = max((i for i, o in enumerate(record.observations) if o.source in _MUTATORS), default=-1)
        for index, obs in enumerate(record.observations):
            parameters = args.get(obs.tool_call_id, {})
            if obs.source == "poll_process":
                if obs.meta.get("process_state") not in {"exited", "timed_out", "cancelled"}:
                    continue
                parameters = {"command": obs.meta.get("command", ""), "cwd": obs.meta.get("cwd", ".")}
                obs = obs.model_copy(update={"source": "run_shell"})
            if obs.source == "run_shell" and obs.meta.get("process_state") == "running":
                continue
            reads = set()
            if obs.source in {"read_file", "read_lines"} and obs.ok:
                path = obs.meta.get("path") or parameters.get("path")
                if path:
                    offset = (obs.meta.get("offset", parameters.get("offset", 0)) if obs.source == "read_file"
                              else ("line", obs.meta.get("start_line", parameters.get("start_line", 1))))
                    path = str(Path(path))
                    if changed and self._current.get(path) != obs.meta.get("sha256") and obs.meta.get("sha256"):
                        continue  # A later tool in this batch replaced the returned page.
                    covered = self._record_page(path, obs)
                    if covered is None:
                        reads.add((path, offset))  # Compatibility with cursor-only receipts.
                    else:
                        advances |= covered
                        self._reads.add((path, offset))
                        if path in self._current:
                            self._inspected.add(path)
            elif obs.source in {"search_files", "find_files", "list_dir"} and obs.ok and obs.content:
                query = json.dumps(parameters, sort_keys=True, ensure_ascii=False)
                advances |= self._navigation_result(obs, query, search=obs.source != "list_dir")
            elif obs.source in {"run_python", "run_shell", "run_browser"}:
                self._clean_exits += int(obs.ok and obs.meta.get("exit_code") == 0)
                signal = invocation = None
                cwd = str(parameters.get("cwd") or ".")
                reading = False
                if obs.source == "run_shell":
                    self._shell_calls += 1
                    invocation = shell_invocation(str(parameters.get("command", "")), cwd)
                    reads, command, reading = _shell_inputs(str(parameters.get("command", "")),
                                                            str(parameters.get("cwd") or "."), self._current)
                    if not obs.ok:
                        reads.clear()
                    elif re.match(r"^(?:ls|rg|grep|find|pwd)(?:\s|$)", str(parameters.get("command", "")).strip()):
                        advances |= self._navigation_result(obs, command, search=True)
                    # A source dump is navigation even if the file is tests.txt.
                    signal = _check(obs, "" if reading else command, reading, None)
                elif obs.source == "run_browser":
                    self._browser_calls += 1
                    signal = _browser_check(obs, str(parameters.get("code", "")), str(parameters.get("cwd", ".")))
                else:
                    self._executions += 1
                    invocation = python_invocation(str(parameters.get("code", "")), cwd)
                    paths, command, exit_position, reading = _script_inputs(str(parameters.get("code", "")))
                    reads.update((p, 0) for p in paths if obs.ok and p in self._current)
                    signal = _check(obs, command, reading, exit_position)
                exit_code = obs.meta.get("exit_code")
                missing_exit = False
                if invocation and invocation.exit_footer and exit_code == 0:
                    stdout = obs.content.partition("\nstderr:\n")[0]
                    footer = re.search(r"(?m)^EXIT=(-?\d{1,8})\s*\Z", stdout)
                    exit_code = int(footer[1]) if footer else None
                    missing_exit = exit_code is None
                    if missing_exit:
                        signal = _Check(command, "inconclusive", "")
                if obs.source == "run_shell" and invocation and invocation.complete and invocation.script:
                    # A custom reporter may be silent or use any language.
                    # This receipt proves process completion, never a passed assertion.
                    self._script_exits.pop(invocation.scope, None)
                    if (not signal and not changed and invocation.script in self._current
                            and obs.ok and type(exit_code) is int and exit_code == 0
                            and not obs.meta.get("timed_out") and not obs.meta.get("cancelled")):
                        self._script_exits[invocation.scope] = {
                            "check": invocation.caption, "path": invocation.script,
                            "sha256": self._current[invocation.script], "exit_code": exit_code,
                            "tool_call_id": obs.tool_call_id}
                        if len(self._script_exits) > 32:
                            del self._script_exits[next(iter(self._script_exits))]
                if (not signal and invocation and invocation.complete and obs.source == "run_shell"
                        and _output_truncated(obs) and obs.ok and exit_code == 0
                        and not obs.meta.get("timed_out") and (
                            not invocation.script or self._current.get(invocation.script) is not None and
                            self._assertive_scripts.get(invocation.script) == self._current[invocation.script])):
                    # The process completed even when its display buffer filled. An
                    # exact exit receipt doesn't resolve any prior named assertion.
                    signal = _Check(command, "runner_exit", invocation.caption + " 完整运行退出 0（输出已截断）")
                if signal:
                    # Trust which process/assertion produced the result before interpreting
                    # its text. A wrapper printing an edited summary has its own stdout.
                    native = obs.source == "run_browser" or (
                        obs.source == "run_python" and command.startswith("assert "))
                    scoped_rows = (obs.source == "run_shell" and signal.cases and
                                   _shell_case_suite(str(parameters.get("command", "")), cwd))
                    interrupted = obs.meta.get("timed_out") or obs.meta.get("cancelled") or obs.meta.get("exit_code") in {-9, -15}
                    # A safe output filter may relay exact individual cases. Its
                    # outer exit code and aggregate totals do not certify a run.
                    full = native or bool(invocation and invocation.complete)
                    trusted_rows = bool(scoped_rows and not full and
                                        (signal.status == "passed" or any(
                                            int(code) != 0 for code in _NESTED_EXIT.findall(obs.content))))
                    # For a direct custom script, stdout claiming failure despite a
                    # verified zero exit is conflicting evidence, not a failed assertion.
                    conflict = (full and invocation and invocation.script in self._current and signal.status == "failed"
                                and obs.source == "run_shell" and exit_code == 0)
                    unavailable = (signal.status == "failed" and invocation and obs.source == "run_shell" and (
                        obs.meta.get("exit_code") in {126, 127} or "pytest" in invocation.caption
                        and not invocation.script and obs.meta.get("exit_code") in {2, 3, 4, 5}))
                    unconfirmed_claim = (full and invocation and invocation.script.endswith(".py")
                                         and invocation.script in self._current and signal.status == "passed"
                                         and signal.cases and self._assertive_scripts.get(invocation.script)
                                         != self._current[invocation.script])
                    if missing_exit or interrupted or unavailable or conflict or unconfirmed_claim or not full and not trusted_rows:
                        reason = ("未取得测试进程的退出码" if missing_exit else
                                  "执行中断，未取得完整检查结果" if interrupted else
                                  "未识别到可执行断言，已保留该脚本的输出" if unconfirmed_claim else
                                  "运行器未能完成测试" if unavailable else
                                  "检查文本与脚本退出状态不一致" if conflict else
                                  "命令包装或输出处理无法归属为完整用例结果")
                        key = (obs.source, reason, invocation.scope if invocation else "")
                        note = {"tool": obs.source, "step": record.step, "reason": reason,
                                "check": invocation.caption if invocation else "",
                                "exit_code": obs.meta.get("exit_code")}
                        self._diagnostic_checks[key] = note
                        if len(self._diagnostic_checks) > 8:
                            del self._diagnostic_checks[next(iter(self._diagnostic_checks))]
                        needs_review = missing_exit or interrupted or unavailable or conflict or signal.status == "failed" and invocation and not full
                        # Wrappers can't introduce a second obligation for the same
                        # script when an observed interrupted launch already names it.
                        covered = invocation and invocation.script and any(
                            item.get("selection") == invocation.selection and not full
                            or item["scope"] == invocation.scope for item in self._pending_verification.values())
                        if (needs_review and invocation and not covered
                                and (not invocation.script or invocation.script in self._current)):
                            self._pending_verification[invocation.scope] = {
                                "scope": invocation.scope, "selection": invocation.selection, "check": invocation.caption, "path": invocation.script,
                                "at_sha256": self._current.get(invocation.script)}
                        signal = None
                    elif full and signal.status in {"passed", "runner_exit"} and invocation and obs.source == "run_shell" and (
                            obs.ok and exit_code == 0
                            and (not changed or index >= last_mutation)):
                        script_digest = self._current.get(invocation.script)
                        eligible = (not invocation.script or script_digest is not None and
                                    self._assertive_scripts.get(invocation.script) == script_digest)
                        if eligible:
                            self._complete_verification[invocation.scope] = invocation.caption
                        prior = self._pending_verification.get(invocation.scope)
                        if eligible and prior and prior.get("at_sha256") == script_digest:
                            del self._pending_verification[invocation.scope]
                if signal:
                    suite, status, label, cases = signal
                    case_suite = suite
                    source = run = None
                    if cases and obs.source == "run_shell":
                        command = str(parameters.get("command", ""))
                        cwd = str(parameters.get("cwd") or ".")
                        attributed = _shell_case_suite(command, cwd)
                        case_suite = attributed or suite
                        if attributed:
                            argv = shlex.split(attributed.removeprefix(f"shell case {Path(cwd)} "))
                            path = str(Path(cwd) / argv[1])
                            if path in self._current:
                                source = (path, self._current[path])
                                if source[1] == before.get(path):
                                    run = _script_run(self._layouts, self._current, command, cwd, obs, status)
                    # Bind a named case to its script bytes even when its checklist
                    # is restored into another member process without the prior AST.
                    versioned = case_suite + "\0sha256=" + source[1] if source else case_suite
                    suite_key = _check_key(suite)
                    if status == "failed":
                        failures = {}
                        for case in cases:
                            caption = run.layout.captions[run.failed[case]] if run and case in run.failed else case
                            failures[_check_key(versioned, caption)] = "工具输出 FAIL · " + case[:72]
                        if not cases:
                            failures[suite_key] = label
                        advances |= not failures.keys() <= self._failures.keys()
                        self._failures.update(failures)
                        for key, failure in failures.items():
                            self._review_evidence["fail:" + key] = {
                                "kind":"failed_check","label":failure,"scope":key,"tool_call_id":obs.tool_call_id}
                        if obs.source == "run_browser" or (obs.source == "run_python" and command.startswith("assert ")):
                            code = parameters.get("code", "")
                            if obs.tool_call_id and 0 < len(code.encode()) <= 6000:
                                self._inline_failures[suite_key] = {"code":code, "error":obs.content[-800:],
                                    "tool":obs.source, "tool_call_id":obs.tool_call_id, "task_id":state.task_id}
                                if len(self._inline_failures) > 16:
                                    del self._inline_failures[next(iter(self._inline_failures))]
                        if run:
                            self._failed_runs[(case_suite, run.digest)] = run
                        self._passes.pop(suite, None)
                        self._repeat_pass = False
                    elif status == "clean_exit":
                        advances |= self._failures.pop(suite_key, None) is not None
                    elif not changed or index >= last_mutation:
                        advances |= suite not in self._passes
                        self._repeat_pass |= self._passes.get(suite) == label
                        if full:
                            self._passes[suite] = label
                            self._review_evidence["pass:" + suite_key] = {
                                "kind":"passed_check","label":label,"scope":suite_key,"tool_call_id":obs.tool_call_id,
                                "asserted":status != "runner_exit"}
                            if status != "runner_exit":
                                self._failures.pop(suite_key, None)
                        for case in cases:
                            self._failures.pop(_check_key(versioned, case), None)
                            # Pre-versioned checklists used only the runner and caption.
                            # Match these only against unchanged, restored script bytes.
                            legacy = _check_key(case_suite, case)
                            if source and self._starting.get(source[0]) == source[1] and legacy in self._restored_keys:
                                self._failures.pop(legacy, None)
                        if run:
                            self._retire_revisions(case_suite, run)
                elif obs.ok and not reading:
                    # Different observed results from real data/API queries
                    # are useful investigation. Repeated clocks and elapsed
                    # test timings alone should not reset a stalled loop.
                    output = obs.content.partition("stdout:\n")[2].strip()
                    if output:
                        normalized = re.sub(r"0x[0-9a-fA-F]+|\b\d+(?:\.\d+)?\s*(?:ms|s)\b", "#", output)
                        fingerprint = hashlib.sha256(normalized.encode()).digest()
                        advances |= fingerprint not in self._investigations
                        self._investigations.add(fingerprint)
            elif obs.source in {"search_sources", "http_get"} and obs.ok:
                if path := obs.meta.get("path"):
                    reads.add((path, 0))
            elif obs.source == "human" and obs.ok:
                advances = True
            advances |= bool(reads - self._reads)
            self._reads.update(reads)
            self._inspected.update(p for p, _ in reads if p in self._current)
        self._quiet = 0 if advances else self._quiet + 1
        # RAO terminates on the fifth identical action. Offer a real final
        # model turn while the member can still describe evidence or blockers.
        if not advances and state.scratch.get("repeat", {}).get("count", 0) >= 4:
            self._stop = "同一动作连续四次且未增加证据"
        self._after_pass = self._after_pass + 1 if self._passes and seen_before and not advances else 0

    def _retire_revisions(self, suite: str, run: _ScriptRun) -> None:
        for identity, prior in list(self._failed_runs.items()):
            if identity[0] != suite or prior.path != run.path:
                continue
            if prior.layout.shape != run.layout.shape or prior.order != run.order:
                continue
            old_suite = suite + "\0sha256=" + prior.digest
            old_keys = []
            for slot in prior.failed.values():
                key = _check_key(old_suite, prior.layout.captions[slot])
                old_keys.append(key)
                if key not in self._failures:
                    continue
                original = self._failures.pop(key)
                if prior.digest != run.digest:
                    entry = {
                        "path": run.path, "prior_sha256": prior.digest, "current_sha256": run.digest,
                        "prior_failure": original, "current_caption": run.layout.captions[slot][:120],
                        "source": "member_created", "observed_case_count": len(run.order),
                        "prior_exit_code": prior.exit_code, "current_exit_code": run.exit_code,
                        "disposition": "superseded_member_check_pending_independent_acceptance",
                    }
                    self._retired.append(entry)
                    self._active_retirements[key] = entry
            if not any(key in self._active_retirements or key in self._failures for key in old_keys):
                del self._failed_runs[identity]


    def _navigation_result(self, obs, query, *, search):
        key = (obs.source, query, hashlib.sha256(obs.content.encode()).digest())
        fresh = key not in self._navigation
        self._navigation.add(key)
        if search:
            self._searches.add(key)
        return fresh

    def _record_page(self, path: str, obs: Observation) -> bool | None:
        meta = obs.meta
        digest, total = meta.get("sha256"), meta.get("chars")
        if obs.source == "read_file":
            start, size = meta.get("offset"), meta.get("returned_chars")
            end = start + size if type(start) is int and type(size) is int else None
        else:
            start, end = meta.get("char_start"), meta.get("char_end")
        if not digest or not all(type(n) is int and n >= 0 for n in (start, end, total)):
            return None
        previous = self._read_coverage.pop(path, None)
        new_version = previous is None or previous.sha256 != digest
        page = _ReadCoverage(digest, total) if new_version else previous
        self._read_coverage[path] = page
        fresh = _cover(page.ranges, min(start, total), min(end, total))
        if obs.source == "read_lines" and meta.get("returned_lines"):
            first = meta["start_line"]
            _cover(page.lines, first, first + meta["returned_lines"])
        page.cursor = {key: meta[key] for key in ("offset", "next_offset", "eof", "start_line", "next_line",
                                                  "total_lines", "partial_line") if key in meta}
        if not fresh and not new_version:
            status = "该版本全文已由读取工具返回。" if page.complete else ""
            self._read_hint = (f"{path} 这次读取的范围已返回过。{status}"
                               "如需核对某处，可定向读取相关行；已有所需上下文时，请继续编辑或运行验证。")
        return fresh or new_version

    def read_checkpoint(self) -> dict:
        return {path: page.receipt() for path, page in list(self._read_coverage.items())[-6:]}

    def failed_checks(self) -> dict[str, str]:
        # Never put an invocation's raw command into a recoverable checklist.
        failures = dict(list(self._failures.items())[:32])
        if len(failures) < len(self._failures):
            failures[_check_key("overflow")] = "更多未解决的失败超出恢复清单容量，需完整复核"
        return failures

    def restore_failed_checks(self, previous: dict[str, str]) -> None:
        if len(previous) > 33 or any(
            not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(value, str) or len(value) > 160
            for key, value in previous.items()
        ):
            raise ValueError("无效的检查恢复清单")
        self._failures.update(previous)
        self._restored_keys.update(previous)

    def restore_pending_verification(self, previous):
        for entry in previous:
            if isinstance(entry, dict) and re.fullmatch(r"[0-9a-f]{64}", str(entry.get("scope", ""))):
                self._pending_verification[entry["scope"]] = {k: entry.get(k) for k in ("scope", "selection", "check", "path", "at_sha256")}

    def require_handoff(self, reason: str) -> None:
        self._stop = reason

    def pending_paths(self) -> frozenset[str]:
        return frozenset(self.required - self._current.keys())

    def resume_delivery(self) -> None:
        """Preserve full editing and checking capabilities on a restored assignment."""
        self._resumed = True

    def _unchanged_outputs(self) -> list[str]:
        return sorted(p for p in self.required & self._current.keys() if self._initial.get(p) == self._current[p])

    def phase(self, budget: Budget, feedback: str = "") -> str:
        if self._stop and feedback.startswith("[验收未通过]") and feedback != self._rejection:
            self._rejection = feedback
            self._stop = ""
            self._repeat_pass = False
            self._passes.clear()
            self._after_pass = self._quiet = 0
        if self._stop:
            return "handoff"
        if not self._steps and not self._resumed:
            return "work"
        if (not budget.has_steps(2) or not budget.has_actions(2)
                or not budget.has_tokens(1) or budget.seconds_left <= 0):
            self._stop = "为最终交接保留本次分工的剩余额度"
        elif self._quiet >= 7:
            self._stop = "连续七轮没有新的文件、来源或检查结果"
        elif (not self._failures and not self.pending_paths() and not self._unchanged_outputs()
              and self._initial != self._current and self._passes and self._quiet
              and (self._repeat_pass or self._after_pass >= 3)):
            self._stop = "已有检查结果，同一版本的复查未增加交付证据"
        return "handoff" if self._stop else "work"

    def blocked_reason(self) -> str | None:
        if self._failures:
            return "仍有检查失败：" + "、".join(sorted(set(self._failures.values()))[:4])
        if self._pending_verification and not (self._passes or self._pending_verification.keys() & self._script_exits.keys()):
            return "本版本尚无正常完成的检查。请直接运行以下检查并保留真实退出码：" + "、".join(
                item["check"] for item in list(self._pending_verification.values())[:4])
        missing = sorted(self.required - self._current.keys())
        if missing:
            return "缺少约定的交付文件：" + "、".join(missing[:8])
        if self._stop and self._unchanged_outputs() and self._initial == self._current and not self._passes:
            return "指定文件沿用分工开始时的公共版本，尚无本轮修改或复验结果"
        if self._stop and self._initial == self._current and not (self._inspected or self._passes or self._searches):
            return "本次分工尚无可交接的文件、已查看来源或检查结果"
        return None

    def _changed_files(self) -> list[str]:
        return sorted(p for p, h in self._current.items() if self._initial.get(p) != h)

    def evidence(self) -> dict:
        return {
            "files": self._changed_files()[:32],
            "removed_files": sorted(self._initial.keys() - self._current.keys())[:16],
            "missing_required_files": sorted(self.pending_paths())[:16],
            "unchanged_required_files": self._unchanged_outputs()[:16],
            "inspected": sorted(self._inspected)[:12],
            "distinct_searches": len(self._searches),
            "check_signals": list(dict.fromkeys(self._passes.values()))[-8:],
            "superseded_member_checks": [dict(entry) for entry in self._retired],
            "diagnostic_checks": list(self._diagnostic_checks.values()),
            "pending_verification": [{**item, "current_sha256": self._current.get(item["path"])}
                                     for item in self._pending_verification.values()],
            "complete_verification": dict(self._complete_verification),
            "script_exits": dict(self._script_exits),
            "observed_failure": list(dict.fromkeys(self._failures.values()))[-4:],
            "stop_reason": self._stop,
            "prior_check_signals_before_edits": self._prior_passes,
            "executions": {"run_python_calls": self._executions, "run_shell_calls": self._shell_calls,
                           "run_browser_calls": self._browser_calls,
                           "outer_exit_zero": self._clean_exits},
        }

    def steering(self) -> str:
        if self._read_hint and not self._stop:
            if not (self._failures or self.pending_paths() or self._unchanged_outputs()):
                return self._read_hint
        if not (self._stop or self._resumed or self._passes or self._failures or self._pending_verification or self._read_hint or self._quiet >= 3
                or self._steps and self.pending_paths()):
            return ""
        evidence = self.evidence()
        if evidence["superseded_member_checks"]:
            evidence = {**evidence, "superseded_member_checks": evidence["superseded_member_checks"][-4:]}
        facts = json.dumps(evidence, ensure_ascii=False)
        if self._read_hint:
            facts += "\n" + self._read_hint
        if self._stop:
            return ("请立即提交最终交接，说明分工成果、文件、实测范围及未完成事项。"
                    "未完成分工或仍有阻塞时以 [BLOCKED] 开头。已观察记录：" + facts)
        if evidence["missing_required_files"] or evidence["unchanged_required_files"]:
            missing = "、".join(evidence["missing_required_files"]) or "无"
            unchanged = "、".join(evidence["unchanged_required_files"]) or "无"
            return (f"本次分工还需交付 {missing}。与分工起点一致的文件：{unchanged}。"
                    "请按分工使用常规工具编辑实现、运行验证，再按真实结果补齐说明。"
                    "已有文件可作输入，只有实际修改和检查才构成本轮交付证据。已观察记录：" + facts)
        if self._pending_verification and not self._failures:
            if self._pending_verification.keys() & self._script_exits.keys():
                return ("已取得对应脚本当前版本的正常退出回执。请交接工件，由独立成员复验原始需求。"
                        "逐项断言仍待确认。已观察记录：" + facts)
            return ("此前的检查曾中断，请在工作目录直接完整运行对应脚本，保留真实退出码和输出。"
                    "已有当前版本的其他完整检查时，可注明未确认范围并交给独立成员复查。已观察记录：" + facts)
        if self._failures:
            return "已有未解决的检查失败。请针对失败修复并重跑相应检查，或明确交接阻塞。已观察记录：" + facts
        return ("新发现和必需文件已齐备时请直接提交最终交接；还有明确缺项请只处理缺项。"
                "请勿重复读取同一文件或重跑已有结果。已观察记录：" + facts)
