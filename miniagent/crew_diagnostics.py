"""Named read-only findings from a blocked member, outside the public project."""
from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from pathlib import Path

from .project_data import validate_project_data
from .schemas import ToolResult
from .tools import Tool


# An explicitly commissioned reviewer program is delivered as a read-only
# attachment. Arbitrary source files from a failed author remain private.
REVIEW_SCRIPT_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".sh", ".go", ".rs", ".java", ".c", ".cc", ".cpp"}


class RoomDiagnostics:
    def __init__(self, root: Path, secret: str):
        self.root = root
        self.secret = secret
        self.files = {}

    def _attachment_bytes(self, assignment, role, path):
        from .studio_service import _read_file
        data, clipped = _read_file(assignment, f'agents/{role}/{path}', 256 * 1024)
        if clipped or not data.strip():
            raise ValueError("附件须为非空文本且不超过 256 KiB。")
        validate_project_data(path, data, secret=self.secret, reject_credentials=True)
        return data

    def missing_review_script(self, assignment, role, paths):
        for path in paths:
            try:
                self._attachment_bytes(assignment, role, path)
            except (ValueError, OSError):
                return f"尚未取得声明的独立检查附件 {path}。请在该路径保存可交接的非空文本（最多 256 KiB）。"
        return None

    def capture(self, assignment, role, assigned, original, task_id, *, checkpoint=None, review_scripts=()):
        from .studio_service import _read_file
        attachments = []
        # Allow named reports and only the separately assigned review programs.
        for path in assigned:
            if (Path(path).suffix.lower() not in {'.md', '.rst', '.txt'}
                    and not (path in review_scripts and Path(path).suffix.lower() in REVIEW_SCRIPT_SUFFIXES)):
                continue
            try:
                data = self._attachment_bytes(assignment, role, path)
                if checkpoint:
                    saved, incomplete = _read_file(checkpoint, f'agents/{role}/{path}', 256 * 1024)
                    if incomplete or saved != data:
                        continue
                sha = hashlib.sha256(data).hexdigest()
                if sha == original.get(path):
                    continue
                text = data.decode('utf-8')
                directory = self.root / 'handoffs'
                directory.mkdir(exist_ok=True)
                identity = uuid.uuid4().hex
                with tempfile.TemporaryDirectory(dir=directory) as scratch:
                    staged = Path(scratch) / 'receipt'
                    dest = staged / 'project' / path
                    dest.parent.mkdir(parents=True)
                    dest.write_bytes(data)
                    (staged / 'manifest.json').write_text(json.dumps({path:sha}), encoding='utf-8')
                    staged.rename(directory / identity)
            except (ValueError, OSError):
                continue
            ref = f'handoffs/{identity}/{path}'
            excerpt = text if len(text) <= 1000 else text[:450] + '\n…\n' + text[-540:]
            attachments.append({'kind':'diagnostic', 'path':ref, 'project_path':path, 'name':Path(path).name,
                                'size':len(data), 'sha256':sha, 'excerpt':excerpt})
            self.files[task_id, path] = (f'handoffs/{identity}/project/{path}', sha)
        return attachments

    def tool(self):
        return Tool('read_handoff_report', '读取本轮群内受阻成员已交接的只读诊断报告。按字符 offset 分页。', {
            'type':'object', 'properties':{'task_id':{'type':'string'}, 'path':{'type':'string'},
                                         'offset':{'type':'integer','minimum':0}},
            'required':['task_id','path'], 'additionalProperties':False}, self.read)

    def read(self, ctx, args):
        from .studio_service import _read_file
        item = self.files.get((args['task_id'], args['path']))
        if not item:
            return ToolResult.failure('当前群没有该任务交接的诊断报告。')
        data, clipped = _read_file(self.root, item[0], 256 * 1024)
        if clipped or hashlib.sha256(data).hexdigest() != item[1]:
            return ToolResult.failure('诊断报告已改变，请查验原始回执。')
        text = data.decode('utf-8')
        start = args.get('offset', 0)
        if start < 0 or start >= len(text):
            return ToolResult.failure('偏移量超过报告范围。')
        # Keep the entire page, including multibyte text, inside the coordinator's
        # existing compact navigation allowance. Offsets always count characters.
        page = text[start:start + 1200].encode('utf-8')[:1200].decode('utf-8', errors='ignore')
        end = start + len(page)
        return ToolResult.success(json.dumps({'task_id':args['task_id'], 'path':args['path'],
            'content':text[start:end], 'next_offset':end if end < len(text) else None}, ensure_ascii=False),
            path=args['path'], sha256=item[1], offset=start)
