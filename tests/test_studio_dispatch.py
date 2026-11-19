"""A handoff accepted in the assistant must produce one identifiable native child."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, fresh, msg
from tests.test_studio_service import finished
from tests.test_studio_superagent import message, tool


def route(target='maintainer', **fields):
    return tool('dispatch_studio_task', target_kind='role', target_id=target, required_participants=[target],
                objective='检查开发环境', **fields)


class StudioDispatchTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir('studio-dispatch')
        self.runs = self.root / 'runs'
        self.settings = Settings(api_key='test-key')
        self.service = StudioService(self.runs, self.settings, max_workers=1)
        self.service.add_profile({'id': 'maintainer', 'name': '小维', 'prompt': '负责开发环境维护。'})

    def tearDown(self):
        self.service.close()
        cleanup(self.root)

    def settled(self, source):
        for _ in range(500):
            rows = self.service.studio_dispatches(source)
            if rows and all(r['status'] not in {'queued', 'running', 'waiting_user'} for r in rows):
                return rows
            time.sleep(0.01)
        self.fail(f'派工未收尾: {rows}')

    def test_single_slot_reuses_only_the_routed_dm_and_transfers_named_input(self):
        other = RecordingProvider([{'text': '私人消息。'}])
        with patch('miniagent.studio_service.DeepSeekProvider', return_value=other):
            secret = self.service.start(msg('private-unrelated-dm', fresh('maintainer')))
            finished(self.service, secret['id'])
        request = route(input_files=['inventory.txt'], acceptance_files=['report.md'])
        first = RecordingProvider([tool('write_file', path='inventory.txt', content='原始盘点。'), request, request,
                                   {'text': '已交给小维。'}])
        child = RecordingProvider([tool('read_file', path='inventory.txt'), tool('write_file', path='report.md', content='开发环境各项检查已完成。'),
                                   {'text': '检查完毕。'}])
        with patch('miniagent.studio_service.DeepSeekProvider', side_effect=[first, child]) as provider:
            source = self.service.send_superagent(message('交给小维检查。'))['id']
            rows = self.settled(source)
            self.assertEqual(provider.call_count, 2)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['status'], 'completed', row)
        self.assertNotEqual(row['conversation_id'], secret['conversation_id'])
        native = self.service.detail(row['child_run_id'])
        self.assertEqual(native['run']['mode'], 'single')
        self.assertEqual(native['run']['recipient'], 'maintainer')
        self.assertEqual(native['conversation']['id'], row['conversation_id'])
        self.assertTrue(native['conversation']['saved_roles'])
        self.assertNotIn('dispatch_studio_task', child.requests[0][1])
        self.assertNotIn('private-unrelated-dm', str(child.requests))
        self.assertIn('原始盘点', str(child.requests))
        self.assertEqual(self.service.artifact(row['child_run_id'], 'agents/maintainer/report.md')['content'], '开发环境各项检查已完成。')
        with patch('miniagent.studio_service.DeepSeekProvider', side_effect=[
                RecordingProvider([route(), {'text': '已交接。'}]), RecordingProvider([{'text': '复核完毕。'}])]):
            again = self.service.send_superagent(message('请小维继续。', source))['id']
            follow, = self.settled(again)
        self.assertEqual(follow['conversation_id'], row['conversation_id'])
        self.assertEqual(self.service.detail(follow['child_run_id'])['conversation']['turns'][0]['run_id'], row['child_run_id'])

    def test_switching_workspace_does_not_reuse_role_project(self):
        rooms = []
        receipts = []
        source = None
        for name in ('project-one', 'project-two'):
            folder = self.root / name
            folder.mkdir()
            (folder / 'selected.txt').write_text(name)
            with patch('miniagent.studio_service.DeepSeekProvider', side_effect=[
                    RecordingProvider([route(), {'text': '已安排。'}]),
                    RecordingProvider([tool('read_file', path='selected.txt'), {'text': '已查看。'}])]):
                source = self.service.send_superagent({**message('让小维检查这个项目。', source), 'workspace_path': str(folder)})['id']
                child, = self.settled(source)
            self.assertIn(child['status'], {'completed', 'unverified'}, child)
            run = self.service.detail(child['child_run_id'])['run']
            self.assertEqual(run['workspace_path'], str(folder.resolve()))
            rooms.append(child['conversation_id'])
            receipts.append(child['dispatch_id'])
        self.assertNotEqual(*rooms)
        source_meta = self.service._meta(self.service._directory(source))
        self.assertIsNone(self.service.studio_dispatch_reference(source_meta, receipts[0]))
        self.assertEqual(self.service.studio_dispatch_reference(source_meta, receipts[1])['child_run_id'],
                         child['child_run_id'])

    def test_restart_drains_committed_queue_and_reattaches_published_child(self):
        parent = RecordingProvider([route(), {'text': '已登记。'}])
        with patch.object(self.service, '_drain_studio_dispatches'), patch('miniagent.studio_service.DeepSeekProvider', return_value=parent):
            source = self.service.send_superagent(message('交给小维。'))['id']
            finished(self.service, source)
            self.assertEqual(self.service.studio_dispatches(source)[0]['status'], 'queued')
            self.service.close()
        with patch('miniagent.studio_service.DeepSeekProvider', return_value=RecordingProvider([{'text': 'Done.'}])) as providers:
            ready = threading.Barrier(2)
            def reopen():
                ready.wait(3)
                return StudioService(self.runs, self.settings, max_workers=1)
            with ThreadPoolExecutor(max_workers=2) as workers:
                left, right = workers.submit(reopen), workers.submit(reopen)
                self.service, other = left.result(timeout=5), right.result(timeout=5)
            try:
                row, = self.settled(source)
                self.assertEqual(other.studio_dispatches(source)[0]['child_run_id'], row['child_run_id'])
                self.assertEqual(providers.call_count, 1)
            finally:
                other.close()
        self.service.close()
        # Simulate a crash after publishing the native run, before acknowledging its outbox entry.
        with closing(sqlite3.connect(self.runs / '.studio-dispatch.sqlite3')) as db, db:
            db.execute("UPDATE dispatches SET state='queued', room=NULL WHERE id=?", (row['dispatch_id'],))
        with patch('miniagent.studio_service.DeepSeekProvider', side_effect=AssertionError('double execution')) as providers:
            self.service = StudioService(self.runs, self.settings, max_workers=1)
            resumed, = self.settled(source)
            self.assertEqual(resumed['child_run_id'], row['child_run_id'])
            self.assertEqual(resumed['conversation_id'], row['conversation_id'])
            self.assertEqual(providers.call_count, 0)

    def test_cancellation_after_acceptance_does_not_start_a_child(self):
        waiting, release = threading.Event(), threading.Event()
        class Held(RecordingProvider):
            def chat(self, messages, tools=None, temperature=0):
                if self.requests:
                    waiting.set()
                    release.wait(4)
                return super().chat(messages, tools, temperature)
        parent = Held([route(), {'text': '已登记。'}])
        with patch('miniagent.studio_service.DeepSeekProvider', return_value=parent) as providers:
            try:
                source = self.service.send_superagent(message('交给小维。'))['id']
                self.assertTrue(waiting.wait(4))
                self.assertEqual(self.service.studio_dispatches(source)[0]['status'], 'queued')
                self.service.cancel(source)
            finally:
                release.set()
            row, = self.settled(source)
            self.assertEqual(row['status'], 'cancelled')
            self.assertIsNone(row['child_run_id'])
            self.assertEqual(providers.call_count, 1)

    def test_group_uses_native_lead_and_first_team_turn(self):
        with patch('miniagent.studio_service.DeepSeekProvider', return_value=RecordingProvider([
                tool('create_studio_group', name='开发组', members=['maintainer', 'reviewer'], lead='maintainer'),
                {'text': '已建群。'}])):
            creator = self.service.send_superagent(message('创建开发组。'))['id']
            history = finished(self.service, creator)['conversation']['turns'][0]['events']
        group = next(event['entity']['id'] for event in history if event.get('entity'))
        parent = RecordingProvider([tool('dispatch_studio_task', target_kind='group', target_id=group,
                                         required_participants=['maintainer','reviewer'],
                                         objective='由负责人安排审阅。'), {'text': '已派发。'}])
        team = RecordingProvider([tool('delegate_task', assignee='reviewer', objective='审阅说明。'),
                                  {'text': '我已审阅。'}, {'text': '已收到审阅。'}])
        with patch('miniagent.studio_service.DeepSeekProvider', side_effect=[parent,team]):
            source = self.service.send_superagent(message('让开发组进行审阅。', creator))['id']
            child, = self.settled(source)
        native = self.service.detail(child['child_run_id'])
        self.assertEqual(child['conversation_id'], group)
        self.assertEqual((native['run']['mode'], native['run']['recipient']), ('team', 'maintainer'))
        self.assertIn(child['status'], {'completed', 'unverified'}, child)
        self.assertEqual(native['conversation']['head_run_id'], child['child_run_id'])
        self.assertTrue(any(e['kind'] == 'assignment' and e['recipient'] == 'reviewer' for e in native['room_events']))
        self.assertNotIn('dispatch_studio_task', team.requests[0][1])

    def test_second_host_queues_busy_role_and_native_completion_delivers_next_turn(self):
        entered, release = threading.Event(), threading.Event()
        class WaitingRole(RecordingProvider):
            def chat(self, messages, tools=None, temperature=0):
                entered.set()
                release.wait(6)
                return super().chat(messages, tools, temperature)
        scripts = [RecordingProvider([route(), {'text': '已交接。'}]), WaitingRole([{'text': '第一次处理完毕。'}]),
                   RecordingProvider([route(), {'text': '已再次交接。'}]), RecordingProvider([{'text': '第二次处理完毕。'}])]
        with patch('miniagent.studio_service.DeepSeekProvider', side_effect=scripts) as provider:
            first = self.service.send_superagent(message('交给小维。'))['id']
            self.assertTrue(entered.wait(3))
            other = StudioService(self.runs, self.settings, max_workers=1)
            try:
                later = other.send_superagent(message('再做一次。', first))['id']
                finished(other, later)
                self.assertEqual(other.studio_dispatches(later)[0]['status'], 'queued')
                release.set()
                first_child, = self.settled(first)
                next_child, = self.settled(later)
                self.assertEqual(next_child['conversation_id'], first_child['conversation_id'])
                self.assertNotEqual(next_child['child_run_id'], first_child['child_run_id'])
                self.assertEqual(provider.call_count, 4)
            finally:
                release.set()
                other.close()
