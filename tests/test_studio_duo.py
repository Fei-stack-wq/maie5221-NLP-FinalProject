"""Room leads can take file-producing assignments while peers retain separate identities."""
import unittest
import hashlib
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_reviews import CandidateLookupError, ReviewQueue, digest
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


class TeamRecipientsTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir('team-recipients')
        self.service = StudioService(self.root, Settings(api_key='test-key'))

    def tearDown(self):
        self.service.close()
        cleanup(self.root)

    def execute(self, roles, script, **kwargs):
        schemas = []
        class Provider(RecordingProvider):
            def chat(self, messages, tools=None, temperature=0):
                for spec in tools or []:
                    if spec.name == 'delegate_task':
                        schemas.append(spec.parameters['properties']['assignee']['enum'])
                return super().chat(messages, tools, temperature)
        with patch('miniagent.studio_service.DeepSeekProvider', return_value=Provider(script)):
            run = self.service.start(request('team', conversation=roster(*roles), objective='核对交付，负责人写报告。', **kwargs))
            return finished(self.service, run['id']), schemas

    def test_two_person_lead_can_author_then_collect_peers_independent_findings(self):
        roles = ('xiaowei', 'reviewer')
        readings = 'LAB-314, Python 3.12.7, available storage 48 GB.'
        create = tool('delegate_task', assignee='xiaowei', objective='整理原文', remaining_roles=['reviewer'],
                      deliverable_paths=['env-report.md'])
        review = tool('delegate_task', assignee='reviewer', objective='读取并核对原文', remaining_roles=['xiaowei'],
                      review={'candidate':'env-report.md','criteria':[{'id':'facts','method':'inspect','description':'核对三项原文'}]})
        sign = tool('submit_review', candidate_version=digest({'env-report.md':hashlib.sha256(readings.encode()).hexdigest()}),
                    results=[{'criterion_id':'facts','verdict':'passed','evidence':['file:env-report.md'],
                              'reason':'三项原文一致，未增添事实。'}])
        summarize = tool('delegate_task', assignee='xiaowei', objective='汇总独立意见', deliverable_paths=['review.md'])
        detail, schemas = self.execute(roles, [create, write('env-report.md', readings), {'text':'报告已保存。'}, review,
            tool('read_file', path='env-report.md'), sign, summarize,
            write('review.md', '独立审阅者已逐项比对原文。结论是各项一致。'), {'text':'已汇总。'}, {'text':'两份文件已交付。'}],
            acceptance_files=['env-report.md','review.md'])
        self.assertEqual(detail['run']['status'], 'completed', detail['diagnostics'])
        self.assertTrue(all(set(s) == set(roles) for s in schemas))
        self.assertEqual([e['recipient'] for e in detail['room_events'] if e['kind']=='assignment'],
                         ['xiaowei','reviewer','xiaowei'])
        self.assertEqual(self.service.artifact(detail['run']['id'], 'agents/xiaowei/env-report.md')['content'], readings)

    def test_planned_independent_peer_cannot_be_replaced_by_self_review(self):
        create = tool('delegate_task', assignee='lead', objective='撰写报告', deliverable_paths=['report.md'],
                      remaining_roles=['reviewer'])
        self_review = tool('delegate_task', assignee='lead', objective='对自己验收', deliverable_paths=['report.md'],
                           review={'candidate':'lead','criteria':[{'id':'r','method':'inspect','description':'check facts'}]})
        detail, _ = self.execute(('lead','reviewer'), [create, write('report.md', 'This report has one author.'),
            {'text':'Done.'}, self_review, self_review, {'text':'All work is done.'}], acceptance_files=['report.md'])
        self.assertEqual(detail['run']['status'], 'failed', detail['run'])
        self.assertEqual([e['recipient'] for e in detail['room_events'] if e['kind']=='assignment'], ['lead'])
        self.assertTrue(any('原作者不能' in e.get('preview','') and e.get('ok') is False for e in detail['events']))

    def test_invalid_larger_room_recipient_gets_real_choices_then_bounded_failure(self):
        invalid = tool('delegate_task', assignee='outsider', objective='写报告', deliverable_paths=['report.md'])
        detail, schemas = self.execute(('chief','writer','reviewer'),[invalid,invalid,{'text':'[BLOCKED] 无法分配'}])
        self.assertEqual(detail['run']['status'], 'failed')
        self.assertTrue(schemas and all(set(s)=={'chief','writer','reviewer'} for s in schemas))
        rows = [e for e in detail['events'] if e['kind']=='tool_result' and e.get('operation')=='delegate_task']
        self.assertEqual(len(rows), 2)
        self.assertTrue(all('writer' in e['preview'] and 'reviewer' in e['preview'] for e in rows))

    def test_research_lead_of_three_writes_then_hands_off_to_writer_and_independent_reviewer(self):
        roles = ('researcher', 'writer', 'reviewer')
        notes, guide, review = 'notes/research.md', 'docs/guide.md', 'docs/review.md'
        detail, schemas = self.execute(roles, [
            tool('delegate_task', assignee='researcher', objective='先调研，保存笔记',
                 remaining_roles=['writer','reviewer'], deliverable_paths=[notes]),
            write(notes, 'A queue and a schema are different parts of a protocol.'), {'text':'调研笔记就绪。'},
            tool('delegate_task', assignee='writer', objective='基于调研笔记撰写教程', deliverable_paths=[guide]),
            tool('read_file', path=notes), write(guide, 'Define the message schema, then choose the queue.'),
            {'text':'已交付成稿。'},
            tool('delegate_task', assignee='reviewer', objective='对照笔记独立复核成稿', deliverable_paths=[review]),
            tool('read_file', path=guide), tool('read_file', path=notes),
            write(review, 'Reviewed: the schema and queue are distinguished consistently.'), {'text':'已复核。'},
            {'text':'三份文件已交付。'},
        ], acceptance_files=[notes,guide,review])
        self.assertEqual(detail['run']['status'], 'completed', detail['diagnostics'])
        self.assertTrue(schemas and all(set(s) == set(roles) for s in schemas))
        published = [event for event in detail['room_events'] if event['kind'] == 'member_report']
        self.assertEqual([row['role'] for row in published], list(roles))
        for row, path in zip(published, [notes,guide,review], strict=True):
            self.assertTrue(row['handoff']['published'])
            self.assertEqual([change['path'] for change in row['handoff']['changed_files']], [path])
        self.assertIn(notes, published[1]['handoff']['evidence']['inspected'])
        self.assertIn(guide, published[2]['handoff']['evidence']['inspected'])

    def test_reviewer_can_return_a_concrete_file_to_the_lead_for_repair(self):
        from tests.test_studio_native_review import PATH, RULE, submit
        old = 'Shipment destination: old warehouse. Verified dispatch record.'
        updated = 'Shipment destination: new warehouse. Verified dispatch record.'
        steps = [
            tool('delegate_task', assignee='lead', objective='Write the label.', deliverable_paths=[PATH],
                 remaining_roles=['inspector']),
            write(PATH, old), {'text':'First label ready.'},
            tool('delegate_task', assignee='inspector', objective='Check the label.',
                 review={'candidate':PATH, 'criteria':[RULE]}),
            tool('read_file', path=PATH),
            submit(old, 'changes_requested', cause='product', action='Print the confirmed new warehouse.'),
            {'text':'Returned to author.'},
            tool('delegate_task', assignee='lead', objective='Correct the returned label.'),
            write(PATH, updated), {'text':'Label updated.'},
            tool('delegate_task', assignee='inspector', objective='Independently verify the revised label.'),
            tool('read_file', path=PATH), submit(updated, 'passed'), {'text':'The new label passes.'},
            {'text':'Delivered the reviewed revision.'},
        ]
        detail, _ = self.execute(('lead','inspector','publisher'), steps, acceptance_files=[PATH], limits={'max_steps':90})
        self.assertEqual(detail['run']['status'], 'completed', detail['diagnostics'])
        reports = [event['handoff'] for event in detail['room_events'] if event['kind'] == 'member_report']
        self.assertEqual([row['role'] for row in reports], ['lead', 'inspector', 'lead', 'inspector'])
        self.assertEqual([row['native_review']['state'] for row in reports[1:]], ['repair','review','passed'])

    def test_three_person_lead_can_complete_the_only_file_assignment(self):
        missing_path = tool('delegate_task', assignee='lead', objective='再分配整组工作')
        create = tool('delegate_task', assignee='lead', objective='整理笔记', remaining_roles=[],
                      deliverable_paths=['notes.md'])
        detail, _ = self.execute(('lead','writer','reviewer'), [
            missing_path, create, write('notes.md','First stage completed.'), {'text':'已交接。'},
            {'text':'已整理笔记。'},
        ], acceptance_files=['notes.md'])
        self.assertEqual(detail['run']['status'], 'completed', detail['diagnostics'])
        self.assertEqual([e['recipient'] for e in detail['room_events'] if e['kind']=='assignment'], ['lead'])
        self.assertTrue(any(e.get('ok') is False and 'deliverable_paths' in e.get('preview','')
                            for e in detail['events']))

    def test_review_path_binds_current_publisher_even_when_lead_and_disallows_self_review(self):
        reviews = ReviewQueue()
        path = 'notice.md'
        signature = hashlib.sha256(b'actual report').hexdigest()
        editor = {'task_id':'author-task', 'role':'lead', 'objective':'write', 'deliverable_paths':[path]}
        auditor = {'task_id':'peer-task', 'role':'reviewer', 'objective':'inspect', 'deliverable_paths':[path]}
        reviews.published(editor, {path:signature}, [{'path':path, 'change':'added', 'sha256':signature}])
        # Reading a document, even when named as an expected deliverable, does not transfer authorship.
        reviews.published(auditor, {path:signature}, [])
        spec = {'candidate':path, 'criteria':[{'id':'facts','method':'inspect','description':'核对实际记录'}]}
        item = reviews.open(spec, 'reviewer', [editor,auditor])
        self.assertEqual((item.author,item.target,item.files),('lead','author-task',{path:signature}))
        with self.assertRaises(CandidateLookupError):
            reviews.open(spec, 'lead', [editor,auditor])
        reviews.published(editor, {}, [{'path':path, 'change':'deleted'}])
        with self.assertRaises(CandidateLookupError):
            reviews.open(spec, 'reviewer', [editor,auditor])
