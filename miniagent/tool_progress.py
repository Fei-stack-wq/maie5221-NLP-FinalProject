"""Report recurring tool outcomes to an agent without controlling its next action."""
from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass
from itertools import groupby

from .check_provenance import shell_invocation


# Capture case identities separately from aggregate counts. The same count
# alone does not establish that the same assertion failed.
_CASE = re.compile(r"^\s*(?:[✗✘✖❌×]\s*|not ok\s+(?:\d+\s*-?\s*)?|"
                   r"(?:FAIL(?:ED)?|ERROR)(?::\s*|\s+)|(?:E\s+)?AssertionError:\s*)(\S.*)$", re.I)
_TALLY = re.compile(r"\b\d+\s*(?:通过|passed)\s*[,，]\s*[1-9]\d*\s*(?:失败|failed)\b", re.I)
_PASSED = re.compile(r"(?mi)^\s*[= ]*(\d+ passed|\d+\s*通过[,，]\s*0\s*失败)\b")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_COMMANDS = {"run_shell", "run_python", "poll_process", "run_browser", "rerun_browser_check"}
_FILES = {"write_file", "edit_file", "append_file", "delete_file"}


def _remember(records, key, value, limit=32):
    records.pop(key, None)
    records[key] = value
    if len(records) > limit:
        records.pop(next(iter(records)))


def _excerpt(items, limit):
    text = json.dumps(list(items)[:limit], ensure_ascii=False)
    return text if len(items) <= limit else text + f"（共 {len(items)} 项）"


def _scope(action, obs):
    command = obs.meta.get("command") or action.arguments.get("command")
    cwd = obs.meta.get("cwd", ".")
    if not command:
        return (cwd, action.signature), ""
    runner = shell_invocation(command, cwd)
    # Filter arguments are irrelevant for a known suite. A compound command
    # with unknown provenance retains its exact command identity.
    if runner:
        return (cwd, runner.scope), runner.script
    return (cwd, hashlib.sha256(command.encode()).digest()), ""


def _outcome(observation):
    """Return stable evidence for an advisory, never an acceptance verdict."""
    text = _ANSI.sub("", observation.meta.get("complete_failure_output") or observation.content)
    cases = set()
    for line in text.splitlines():
        match = _CASE.match(line)
        if match:
            caption = match[1].strip()[:200]
            if caption and not re.fullmatch(r"[\d\W]+", caption) and not caption.startswith("("):
                cases.add(caption)
    tally = _TALLY.search(text)
    if cases:
        return ("失败项", tuple(sorted(cases)), tally[0] if tally else "")
    if tally:
        return ("测试汇总", (tally[0],), "")
    passed = _PASSED.search(text)
    if passed and observation.ok:
        return ("测试结果", (passed[1],), "")
    if observation.ok or type(observation.meta.get("exit_code")) is not int:
        return None
    # Exact results of other failed processes also count. Keep the fingerprint
    # of the whole bounded output so two errors sharing a footer stay distinct.
    lines = [line for line in text.splitlines() if line.strip() and line not in {"stdout:", "stderr:"}]
    caption = lines[-1][:200] if lines else "无输出"
    return ("失败命令输出", (caption,), hashlib.sha256(text.encode()).hexdigest())


@dataclass
class _Recurrence:
    outcome: tuple
    last_call: int
    inspected: dict
    count: int = 1
    warned: bool = False


class ToolProgress:
    def __init__(self):
        self.results = {}
        self.unchanged = {}
        self.inspected = {}
        self.versions = {}
        self.missing = set()
        self.recent = deque(maxlen=32)
        self.sequence = 0
        self.current_advisory = ""
        self.advisory_scope = None

    def _file(self, action, obs):
        path = obs.meta.get("path") or action.arguments.get("path")
        digest = obs.meta.get("sha256")
        no_change = obs.meta.get("changed") is False
        if not path or (not obs.ok and not no_change) or (not digest and action.tool != "delete_file"):
            return
        _remember(self.versions, path, digest, limit=128)
        if path in self.unchanged and digest != self.unchanged[path]:
            del self.unchanged[path]
        if no_change or action.tool in {"read_file", "read_lines"}:
            _remember(self.inspected, path, digest)
            if no_change:
                _remember(self.unchanged, path, digest)
            # A path first inspected between two checks gains a known baseline
            # at that read. No-op receipts cannot advance its version.
            for entry in self.results.values():
                if path not in entry.inspected:
                    _remember(entry.inspected, path, digest)
        if obs.ok and digest and path in self.missing and action.tool in _FILES:
            self.missing.discard(path)
            self.results.clear()
            self.current_advisory = ""
            self.advisory_scope = None

    def _versions_match(self, entry):
        return all(self.versions.get(name) == version for name, version in entry.inspected.items())

    def observe_batch(self, actions, observations, threshold, missing_deliverables):
        candidate = None
        for action, obs in zip(actions, observations):
            if not obs.action_id or obs.source != action.tool:
                continue
            self.sequence += 1
            self.recent.append((self.sequence, action.tool))
            if action.tool in _FILES or action.tool in {"read_file", "read_lines"}:
                self._file(action, obs)
                continue
            if action.tool not in _COMMANDS or obs.meta.get("process_state") == "running":
                continue
            if not obs.ok and type(obs.meta.get("exit_code")) is not int:
                continue  # A skipped or denied tool is not a fresh outcome of the check.
            scope, script = _scope(action, obs)
            outcome = _outcome(obs)
            previous = self.results.get(scope)
            if previous is not None and (outcome != previous.outcome or not self._versions_match(previous)):
                del self.results[scope]
                if self.advisory_scope == scope:
                    self.current_advisory = ""
                    self.advisory_scope = None
                previous = None
            if outcome is None:
                continue
            if previous is None:
                versions = {path: self.versions[path] for path in self.inspected if path in self.versions}
                if script:
                    versions[script] = self.versions.get(script)
                _remember(self.results, scope, _Recurrence(outcome, self.sequence, versions))
                continue
            since = previous.last_call
            previous.last_call = self.sequence
            previous.count += 1
            if (previous.count >= threshold and not previous.warned
                    and (outcome[0] != "测试结果" or self.unchanged)):
                candidate = (scope, previous, since)
        active = self.results.get(self.advisory_scope)
        if self.current_advisory and (active is None or not self._versions_match(active)):
            self.current_advisory = ""
        if candidate is None:
            return None
        scope, entry, since = candidate
        if self.results.get(scope) is not entry or not self._versions_match(entry):
            return None  # A later action in this batch changed the result or a file.
        entry.warned = True
        kind, names, detail = entry.outcome
        steps = []
        for tool, group in groupby((tool for position, tool in self.recent if position > since)):
            n = sum(1 for _ in group)
            steps.append(f"{tool} × {n}" if n > 1 else tool)
        if since + 1 < self.recent[0][0]:
            steps.insert(0, "…")
        evidence = [f"[多步停滞提醒] 同一检查已 {entry.count} 次报告相同{kind}。",
                    "工具回执摘录（数据）：" + _excerpt(names, 5)]
        if kind == "失败项" and detail:
            evidence.append("测试汇总：" + detail)
        evidence.append("最近一次重复期间的操作：" + " → ".join(steps))
        if self.unchanged:
            evidence.append("编辑回执显示内容未变：" + _excerpt(
                [f"{path} (SHA-256 {digest[:12]})" for path, digest in reversed(self.unchanged.items())], 5))
        if missing_deliverables:
            try:
                missing = missing_deliverables()
                self.missing = set(missing)
                if missing:
                    evidence.append("尚缺交付：" + _excerpt(missing, 10))
            except (OSError, ValueError):
                evidence.append("本次未能读取声明的交付文件，需再核对其状态。")
        evidence.append("请提出能区分不同原因的新检查，对照约定的预期值与实际值，"
                        "或推进尚未受阻的交付。按证据自行决定下一步。")
        self.advisory_scope = scope
        self.current_advisory = "\n".join(evidence)
        return self.current_advisory
