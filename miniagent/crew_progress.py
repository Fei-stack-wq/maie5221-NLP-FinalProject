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

from .project_data import PROJECT_CACHE_DIRS
from .rao import Budget, Observation, RAOState, StepRecord


_UNIT = re.compile(r"\bRan (\d{1,8}) tests? in [\d.]+s\b")
_UNIT_OK = re.compile(r"^OK(?: \([^\n]*\))?\s*$", re.M)
_PYTEST = re.compile(r"(?m)^\s*=*\s*(?:\d+ (?:skipped|warnings?), )*(\d{1,8}) passed\b[^\n]*$")
_FAILURE = re.compile(r"(?m)^\s*(?:=+\s*)?(?:FAILED\s*\(|(?:\d+ (?:passed|skipped|warnings?), )*"
                      r"[1-9]\d* (?:failed|errors?)\b|FAILED \S+::)")
_CASE = re.compile(r"(?im)^\s*(PASS|FAIL)\s*[:|–-]?\s+([^\n]{1,160})")
_NESTED_EXIT = re.compile(r"(?im)(?:^\s*|CompletedProcess\([^\n]*?,\s*)(?:returncode|exit[_ ]?code)\s*[=:]\s*(-?\d{1,8})\b")
_TRACEBACK = re.compile(r"(?ms)^Traceback \(most recent call last\):\s*\n\s+File .+?"
                        r"^(?:[\w.]+(?:Error|Exception)|SystemExit)(?::|$)")
_MUTATORS = {"search_sources", "http_get", "run_python", "run_shell", "run_browser", "write_file", "edit_file", "append_file", "delete_file"}
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
    caption = re.split(r"\s{2,}|\s+->\s+", case.strip(), maxsplit=1)[0]
    return hashlib.sha256((suite + "\0" + caption).encode()).hexdigest()


def _check(obs: Observation, command: str, reading: bool, exit_position: int | None) -> _Check | None:
    # Ignore lines that merely render source, e.g. print(Path(...).read_text()).
    # A known check command which reads input fixtures may still emit a result.
    is_test = bool(re.search(r"(?i)\b(?:pytest|unittest|jest|vitest)\b|(?:^|[/\s_.-])tests?(?:$|[/\s_.-])", command))
    inline_assertions = command.startswith("assert ")
    if reading and not (is_test or inline_assertions):
        return None
    output = obs.content.partition("\n")[2] if obs.content.startswith("exit_code=") else obs.content
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
    cases = _CASE.findall(output)
    failed_cases = [name.strip()[:72] for status, name in cases if status.upper() == "FAIL"]
    passed_cases = [name.strip()[:72] for status, name in cases if status.upper() == "PASS"]
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
        return _Check(suite, "failed", "工具输出 FAIL · " + failed_cases[0], tuple(failed_cases))
    if failure or nested_exception or (check_context and bad_exit):
        return _Check(suite, "failed", f"{framework} 执行失败")
    if obs.truncated or not obs.ok or "截断" in output[-80:]:
        return None
    if unit and int(unit[1]) and _UNIT_OK.search(output[unit.end():]):
        return _Check(suite, "passed", f"unittest · {unit[1]} tests OK", tuple(passed_cases))
    if passed and int(passed[1]):
        return _Check(suite, "passed", f"pytest · {passed[1]} passed", tuple(passed_cases))
    if passed_cases:
        return _Check(suite, "passed", f"工具输出 {len(passed_cases)} 项 PASS · " + " / ".join(passed_cases[:2]), tuple(passed_cases))
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
        self._current = self._scan()
        # A recovered member starts from its private checkpoint, while work
        # credited to the assignment is always relative to the public project.
        self._initial = self._current.copy() if public_baseline is None else dict(public_baseline)
        self._reads = set()
        self._read_coverage: dict[str, _ReadCoverage] = {}
        self._read_hint = ""
        self._inspected = set()
        self._passes = {}
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
                except OSError:
                    continue
        return files

    def observe(self, record: StepRecord, state: RAOState) -> None:
        self._steps += 1
        if not record.observations:
            return
        self._read_hint = ""
        changed = False
        if any(o.source in _MUTATORS for o in record.observations):
            files = self._scan()
            changed = files != self._current
            self._current = files
        if changed:
            self._prior_passes = list(dict.fromkeys([*self._prior_passes, *self._passes.values()]))[-4:]
            self._passes.clear()
            self._repeat_pass = False
            self._after_pass = 0
            self._reads.clear()
            self._investigations.clear()
            for path, coverage in list(self._read_coverage.items()):
                if self._current.get(path) != coverage.sha256:
                    del self._read_coverage[path]
        seen_before = bool(self._passes)
        advances = changed
        args = _tool_args(state)
        last_mutation = max((i for i, o in enumerate(record.observations) if o.source in _MUTATORS), default=-1)
        for index, obs in enumerate(record.observations):
            parameters = args.get(obs.tool_call_id, {})
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
                signal = None
                reading = False
                if obs.source == "run_shell":
                    self._shell_calls += 1
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
                    paths, command, exit_position, reading = _script_inputs(str(parameters.get("code", "")))
                    reads.update((p, 0) for p in paths if obs.ok and p in self._current)
                    signal = _check(obs, command, reading, exit_position)
                if signal:
                    suite, status, label, cases = signal
                    suite_key = _check_key(suite)
                    if status == "failed":
                        failures = ({_check_key(suite, case): "工具输出 FAIL · " + case for case in cases}
                                    if cases else {suite_key: label})
                        advances |= not failures.keys() <= self._failures.keys()
                        self._failures.update(failures)
                        self._passes.pop(suite, None)
                        self._repeat_pass = False
                    elif status == "clean_exit":
                        advances |= self._failures.pop(suite_key, None) is not None
                    elif not changed or index >= last_mutation:
                        advances |= suite not in self._passes
                        self._repeat_pass |= self._passes.get(suite) == label
                        self._passes[suite] = label
                        self._failures.pop(suite_key, None)
                        for case in cases:
                            self._failures.pop(_check_key(suite, case), None)
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
        if (budget.steps_left <= 1 or budget.actions_left <= 1
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
        if not (self._stop or self._resumed or self._passes or self._failures or self._read_hint or self._quiet >= 3
                or self._steps and self.pending_paths()):
            return ""
        evidence = self.evidence()
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
        if self._failures:
            return "已有未解决的检查失败。请针对失败修复并重跑相应检查，或明确交接阻塞。已观察记录：" + facts
        return ("新发现和必需文件已齐备时请直接提交最终交接；还有明确缺项请只处理缺项。"
                "请勿重复读取同一文件或重跑已有结果。已观察记录：" + facts)
