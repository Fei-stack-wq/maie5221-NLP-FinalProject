"""One opt-in rerun of a peer's frozen check using the member's normal shell."""
import re
from dataclasses import replace
from pathlib import Path

from .check_provenance import shell_invocation
from .schemas import ToolResult
from .tools import ToolRegistry


class ClosingPeerChecks:
    def __init__(self, registry, workspace, versions):
        self.registry = registry
        self.shell = registry.get("run_shell")
        self.workspace = workspace.resolve()
        self.versions = versions
        self.offered = set()
        self.narrowed = False

    def restore_shell(self):
        if self.narrowed:
            self.registry.add(self.shell)
            self.narrowed = False

    def offer(self, requests, target, frozen, evidence):
        if not self.shell or not target:
            return None
        for item in requests:
            if item["task"] != target:
                continue
            for entry in item.get("verification", []):
                scope = entry["scope"]
                path, digest = entry.get("path"), entry.get("current_sha256")
                if not path or not digest or item.get("candidate", {}).get(path) != digest or frozen.get(path) != digest:
                    continue
                receipt = evidence["script_exits"].get(scope, {})
                if (scope in evidence["complete_verification"]
                        or (receipt.get("path"), receipt.get("sha256"), receipt.get("exit_code")) == (path, digest, 0)):
                    continue
                # Captions redact arbitrary argv. Keep the actual script option
                # and require the recovered command to match the frozen scope.
                caption = entry.get("check", "")
                if caption == path and re.fullmatch(r"[\w./-]+\.py", path):
                    # Older direct Python receipts exposed only the script path.
                    legacy = shell_invocation("python3 " + path + " -v", ".")
                    if legacy and legacy.scope == scope:
                        caption = legacy.caption
                match = re.fullmatch(r"(python(?:3(?:\.\d+)?)?|node) ([\w./-]+)( -v)?(?: \(cwd=([\w./-]+)\))?", caption)
                if not match:
                    continue
                interpreter, script, flags, cwd = match.groups()
                cwd = cwd or "."
                if script.startswith("-") or (flags and interpreter == "node") or Path(cwd).is_absolute() or ".." in Path(cwd).parts:
                    continue
                command = interpreter + " " + script + (flags or "")
                invocation = shell_invocation(command, cwd)
                if not invocation or (invocation.scope, invocation.script) != (scope, path):
                    continue
                if any(k in frozen and frozen[k] != v for k, v in item["candidate"].items()):
                    continue
                candidate = {**frozen, **item["candidate"]}
                key = (target, tuple(sorted(candidate.items())), scope)
                if key in self.offered:
                    continue

                def matches():
                    versions = self.versions()
                    return all(versions.get(k) == v for k, v in candidate.items())

                if not matches():
                    continue
                self.offered.add(key)
                original = ToolRegistry([self.shell])
                used = False

                def exact(ctx, args):
                    nonlocal used
                    if (used or ctx.sandbox.root != self.workspace or args.get("command") != command
                            or args.get("cwd", ".") != cwd or set(args) - {"command", "cwd"} or not matches()):
                        return ToolResult.failure("当前仅开放本次分工已绑定的命令、目录与文件版本。")
                    used = True
                    return original.dispatch("run_shell", args, ctx)

                schema = {"type": "object", "properties": {
                    "command": {"type": "string", "enum": [command]},
                    "cwd": {"type": "string", "enum": [cwd]}},
                    "required": ["command"] if cwd == "." else ["command", "cwd"], "additionalProperties": False}
                self.registry.add(replace(self.shell, parameters=schema, fn=exact))
                self.narrowed = True
                return ("收尾前，这位作者的指定检查缺少当前版本的独立执行回执。如愿复验，请用 run_shell 运行 "
                        + command + "，目录 " + cwd + "。也可直接交接具体缺口。")
        return None
