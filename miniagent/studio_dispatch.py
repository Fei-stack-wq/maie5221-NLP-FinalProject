"""Durable, host-owned handoffs from the Studio assistant to native Studio runs."""
from __future__ import annotations

import json
import sqlite3
import stat
import uuid
from contextlib import closing, contextmanager
from datetime import datetime

from .studio_conversations import ROLE_LABELS


class StudioDispatch:
    @property
    def _dispatch_path(self):
        return self.runs_dir / '.studio-dispatch.sqlite3'

    @contextmanager
    def _dispatch_db(self, *, write=False):
        from .studio_service import StudioError
        path = self._dispatch_path
        for suffix in ('', '-journal', '-shm', '-wal'):
            try:
                info = path.with_name(path.name + suffix).lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StudioError('派工存储不可读取。', 'storage_error', 409)
        with closing(sqlite3.connect(path.as_uri() + ('?mode=rwc' if write else '?mode=ro'), uri=True,
                                     timeout=10, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            if write:
                db.execute('BEGIN IMMEDIATE')
                db.execute('''CREATE TABLE IF NOT EXISTS dispatches (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, scope TEXT NOT NULL,
                    target_kind TEXT NOT NULL, target_id TEXT NOT NULL, target_name TEXT NOT NULL,
                    objective TEXT NOT NULL, inputs TEXT NOT NULL, acceptance TEXT NOT NULL,
                    child TEXT NOT NULL UNIQUE, room TEXT, state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                    participants TEXT, attachment_ids TEXT NOT NULL DEFAULT '[]')''')
                columns = {c['name'] for c in db.execute('PRAGMA table_info(dispatches)')}
                if 'participants' not in columns:
                    db.execute("ALTER TABLE dispatches ADD COLUMN participants TEXT")
                if 'attachment_ids' not in columns:
                    db.execute("ALTER TABLE dispatches ADD COLUMN attachment_ids TEXT NOT NULL DEFAULT '[]'")
                db.execute('CREATE INDEX IF NOT EXISTS dispatch_source ON dispatches(source)')
                db.execute('CREATE INDEX IF NOT EXISTS dispatch_pending ON dispatches(state)')
            else:
                db.execute('PRAGMA query_only=ON')
            yield db
            if write:
                db.commit()

    def _dispatch_target(self, source, kind, target, participants=()):
        from .studio_service import StudioError
        if not isinstance(target, str) or not target:
            raise StudioError('请提供派工目标的 ID。')
        if kind == 'role':
            if set(participants) - {target}:
                raise StudioError('单人派工只能交给指定的角色。', 'missing_members', 409)
            if target in ROLE_LABELS:
                return ROLE_LABELS[target], target
            return self.current_role(target)['name'], target
        if kind == 'group':
            card = self.studio_routing_group(source, target)
            if card:
                if set(participants) - set(card['members']):
                    raise StudioError('目标群当前缺少请求的参与角色。', 'missing_members', 409)
                return card['name'], card['lead']
            raise StudioError('未找到当前项目可用的命名群聊。', 'unknown_route', 400)
        raise StudioError('派工目标应为角色或群聊。')

    @staticmethod
    def _dispatch_scope(source):
        thread = source['thread']
        return json.dumps([thread['id'], source.get('workspace_path'), thread.get('knowledge_project_id')],
                          ensure_ascii=False)

    def request_studio_dispatch(self, source_run_id, *, target_kind, target_id, objective,
                                acceptance_files=None, input_files=None, required_participants=None, attachment_ids=None):
        from .studio_conversations import conversation_attachments, turn_attachments
        from .studio_route_results import bind_inputs
        from .studio_service import StudioError, _relative
        from .studio_uploads import UploadError, validate_upload_ids
        inputs = [] if input_files is None else input_files
        acceptance = [] if acceptance_files is None else acceptance_files
        if not isinstance(inputs, list) or len(inputs) > 20 or not all(isinstance(path, str) for path in inputs):
            raise StudioError('派工输入需要最多 20 个项目相对路径。')
        for path in inputs:
            _relative(path)
        if len(inputs) != len(set(inputs)):
            raise StudioError('派工输入包含重复文件。')
        with self._lock:
            source = self._meta(self._directory(source_run_id))
            thread = source.get('thread', {})
            if (thread.get('superagent') is not True or source_run_id not in self._active
                    or self._cancellations[source_run_id].is_set()):
                raise StudioError('派工只能由正在执行的统一对话发起。', 'inactive_source', 409)
            inbox = self._steering.get(source_run_id)
            current = turn_attachments({'message_attachments': source.get('attachments', []),
                                        'steering': inbox.rows if inbox else []})
            current_ids = [item['id'] for item in current]
            try:
                attached = validate_upload_ids(current_ids if attachment_ids is None else attachment_ids)
            except UploadError as exc:
                raise StudioError(str(exc), exc.code, exc.status) from None
            if set(attached) - set(current_ids):
                history = self._conversation(self._directory(source_run_id), before=thread['ordinal'], with_answers=False)
                visible = conversation_attachments(history['turns'], source)
                if set(attached) - set(current_ids) - {item['id'] for item in visible}:
                    raise StudioError('只能转交当前项目对话中可见的附件。', 'invalid_attachment')
            participants = [] if required_participants is None else required_participants
            name, _ = self._dispatch_target(source, target_kind, target_id, participants)
            inputs = bind_inputs(self, self._directory(source_run_id), source, inputs)
            # Use the exact same objective and acceptance grammar as native runs.
            checked = self._request({'mode': 'single', 'request_id': str(uuid.uuid4()),
                                     'objective': objective, 'acceptance_files': acceptance})
            objective = checked['objective']
            signature = json.dumps([source_run_id, target_kind, target_id, objective], ensure_ascii=False)
            identity = str(uuid.uuid5(uuid.NAMESPACE_URL, signature))
            scope = self._dispatch_scope(source)
            with self._dispatch_db(write=True) as db:
                row = db.execute('SELECT * FROM dispatches WHERE id=?', (identity,)).fetchone()
                if row is not None and row['state'] == 'cancelled' and inbox and inbox.rows:
                    # A revised instruction can keep the objective while changing
                    # inputs. Keep the cancelled receipt and deduplicate its replacement.
                    identity = str(uuid.uuid5(uuid.UUID(identity), inbox.rows[-1]['request_id']))
                    row = db.execute('SELECT * FROM dispatches WHERE id=?', (identity,)).fetchone()
                if row is not None:
                    if (json.loads(row['attachment_ids']) != attached or set(json.loads(row['inputs'])) != set(inputs)
                            or set(json.loads(row['acceptance'])) != set(acceptance)
                            or row['participants'] is not None and set(json.loads(row['participants'])) != set(participants)):
                        raise StudioError('同一派工已登记，但输入或验收条件不同。', 'dispatch_conflict', 409)
                    if row['state'] == 'queued' and row['participants'] is None:
                        db.execute('UPDATE dispatches SET participants=? WHERE id=?', (json.dumps(participants), identity))
                else:
                    child = 'studio-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:12]
                    db.execute('''INSERT INTO dispatches
                        (id,source,scope,target_kind,target_id,target_name,objective,inputs,acceptance,child,participants,attachment_ids,state)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'queued')''', (identity, source_run_id, scope, target_kind,
                            target_id, name, objective, json.dumps(inputs), json.dumps(acceptance), child, json.dumps(participants),
                            json.dumps(attached)))
                    row = db.execute('SELECT * FROM dispatches WHERE id=?', (identity,)).fetchone()
                return self._dispatch_receipt(row)

    def cancel_queued_dispatch(self, source_run_id, dispatch_id):
        from .studio_service import StudioError
        with self._lock, self._dispatch_db(write=True) as db:
            row = db.execute("SELECT * FROM dispatches WHERE id=? AND source=?",
                             (dispatch_id, source_run_id)).fetchone()
            if source_run_id not in self._active or row is None:
                raise StudioError("本轮没有此派工回执。", "unknown_dispatch")
            if row["state"] not in {"queued", "cancelled"}:
                raise StudioError("派工已离开队列，请在接手方的对话中补充要求。", "dispatch_started", 409)
            db.execute("UPDATE dispatches SET state='cancelled', error=? WHERE id=? AND state='queued'",
                       ("统一助手已撤回排队中的派工。", dispatch_id))
            return self._dispatch_receipt(db.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone())

    def _dispatch_receipt(self, row):
        from .studio_service import StudioError
        status, error, child = row['state'], row['error'], None
        if status == 'started':
            child = row['child']
            try:
                path = self._directory(child)
                meta = self._meta(path)
                if meta.get('studio_dispatch') != {'dispatch_id': row['id'], 'source_run_id': row['source']}:
                    raise StudioError('派工与运行记录不一致。')
                status = meta['state']
                if status in {'queued', 'running'}:
                    if child not in self._active and not self._assistant_elsewhere(path):
                        status = 'interrupted'
                    elif any(rid == child and value['answer'] is None for (rid, _), value in self._questions.items()):
                        status = 'waiting_user'
                error = meta.get('error', '')
            except (StudioError, KeyError):
                child, status, error = None, 'failed', '无法核对已派发的运行记录。'
        return self._redact({'dispatch_id': row['id'], 'source_run_id': row['source'],
            'target_kind': row['target_kind'], 'target_id': row['target_id'], 'target_name': row['target_name'],
            'objective': row['objective'], 'input_files': json.loads(row['inputs']),
            **({'attachment_ids': json.loads(row['attachment_ids'])} if 'attachment_ids' in row.keys() and row['attachment_ids'] != '[]' else {}),
            'acceptance_files': json.loads(row['acceptance']), 'status': status,
            'child_run_id': child, 'conversation_id': row['room'] if child else None, 'error': error})

    def _read_dispatch_rows(self, query, parameters=()):
        if not self._dispatch_path.exists():
            return []
        with self._dispatch_db() as db:
            # Another process can see the new file before its first transaction commits.
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dispatches'").fetchone():
                return []
            return db.execute(query, parameters).fetchall()

    def studio_dispatches(self, source_run_id):
        with self._lock:
            rows = self._read_dispatch_rows('SELECT * FROM dispatches WHERE source=? ORDER BY rowid', (source_run_id,))
            return [self._dispatch_receipt(row) for row in rows]

    def studio_dispatch_reference(self, source_meta, dispatch_id):
        if source_meta.get('thread', {}).get('superagent') is not True:
            return None
        with self._lock:
            rows = self._read_dispatch_rows('SELECT * FROM dispatches WHERE id=? AND scope=?',
                                            (dispatch_id, self._dispatch_scope(source_meta)))
            return self._dispatch_receipt(rows[0]) if rows else None

    def _dispatch_conversation(self, db, row, source, recipient):
        from .studio_service import StudioError
        if row['target_kind'] == 'group':
            room_id = row['target_id']
        else:
            # Look up only DMs established by a previous handoff for this project.
            # A role's unrelated private conversations must never enter this context.
            previous = db.execute('''SELECT room FROM dispatches WHERE scope=? AND target_kind='role'
                AND target_id=? AND state='started' ORDER BY rowid DESC LIMIT 1''',
                (row['scope'], row['target_id'])).fetchone()
            if previous is None:
                return {'intent': 'edit', 'kind': 'dm', 'members': [recipient], 'recipient': recipient}
            room_id = previous['room']
        room = self._conversation(self._directory(room_id), with_answers=False)
        if room and row['target_kind'] == 'group':
            recipient = room['lead']
        if (not room or room.get('superagent') or recipient not in room['members']
                or room['workspace_path'] != source.get('workspace_path')):
            raise StudioError('派工目标会话与当前项目不一致。', 'unknown_route', 409)
        if not room['can_continue']:
            if room['turns'] and room['turns'][-1]['status'] in {'queued', 'running', 'waiting_user'}:
                raise StudioError('目标正在执行，派工已排队。', 'target_busy', 409)
            raise StudioError('目标会话中断，请先在目标会话核对执行状态。', 'target_interrupted', 409)
        return {'intent': 'edit', 'recipient': recipient, 'parent_run_id': room['head_run_id']}

    def _deliver_studio_dispatch(self, db, row):
        from .studio_service import StudioError
        pointer = {'dispatch_id': row['id'], 'source_run_id': row['source']}
        directory = self.runs_dir / row['child']
        # The process may have died after publishing the child but before recording its ID.
        # Reconcile that exact native run. Its tools must never be replayed here.
        if directory.exists():
            meta = self._meta(self._directory(row['child']))
            if meta.get('studio_dispatch') != pointer:
                raise StudioError('预留的派工运行 ID 已被其他任务占用。', 'dispatch_conflict', 409)
            room_id = meta['thread']['id']
        else:
            source_path = self._directory(row['source'])
            source = self._meta(source_path)
            if source.get('cancel_requested') or source['state'] == 'cancelled':
                db.execute("UPDATE dispatches SET state='cancelled', error=? WHERE id=?", ('来源任务已取消。', row['id']))
                return
            if source['state'] in {'queued', 'running'}:
                if row['source'] in self._active or self._assistant_elsewhere(source_path):
                    return
                raise StudioError('派工来源已中断，尚未派发。', 'source_interrupted', 409)
            if source['state'] not in {'completed', 'unverified'}:
                raise StudioError('派工来源执行失败，尚未派发。', 'failed_source', 409)
            _, recipient = self._dispatch_target(source, row['target_kind'], row['target_id'])
            message = self._dispatch_conversation(db, row, source, recipient)
            payload = {'request_id': row['id'], 'mode': 'team' if row['target_kind'] == 'group' else 'single',
                       'objective': row['objective'], 'acceptance_files': json.loads(row['acceptance']),
                       'conversation': message, 'thinking': source['request'].get('thinking', self.settings.thinking),
                       'limits': source['request'].get('limits', {}),
                       'interaction_mode': source['request'].get('interaction_mode', 'interactive')}
            if source.get('workspace_path'):
                payload['workspace_path'] = source['workspace_path']
            if attachment_ids := json.loads(row['attachment_ids']):
                payload['attachments'] = attachment_ids
            group = ({'group_id': row['target_id'], 'participants': json.loads(row['participants'] or '[]')}
                     if row['target_kind'] == 'group' else {})
            native = self.start(payload, routed={**pointer, **group, 'child_run_id': row['child'],
                                                 'input_files': json.loads(row['inputs'])})
            room_id = native['conversation_id']
        db.execute("UPDATE dispatches SET state='started', room=? WHERE id=?", (room_id, row['id']))

    def _drain_studio_dispatches(self):
        from .studio_service import StudioError
        if not self._dispatch_path.exists():
            return
        # Local lock precedes the cross-process outbox transaction everywhere. Admission
        # serializes actual publication of runs, including ordinary GUI submissions.
        with self._lock:
            if self._closed:
                return
            pending = self._read_dispatch_rows("SELECT id FROM dispatches WHERE state='queued' ORDER BY rowid")
            for pending_row in pending:
                identity = pending_row['id']
                with self._dispatch_db(write=True) as db:
                    row = db.execute("SELECT * FROM dispatches WHERE id=? AND state='queued'", (identity,)).fetchone()
                    if row is None:
                        continue
                    try:
                        self._deliver_studio_dispatch(db, row)
                    except StudioError as exc:
                        if exc.code in {'busy', 'provider_unavailable'}:
                            # A free slot or a restart with provider credentials wakes the outbox.
                            break
                        if exc.code not in {'target_busy', 'stale_head'}:
                            db.execute("UPDATE dispatches SET state='failed', error=? WHERE id=?", (str(exc), identity))
                    except (ValueError, OSError, sqlite3.Error):
                        db.execute("UPDATE dispatches SET state='failed', error=? WHERE id=?", ('无法读取派工数据或准备输入文件。', identity))
