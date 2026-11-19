"""Two-person rooms may attribute a concrete deliverable to their lead."""
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

    def test_lead_handoff_alone_cannot_finish_and_cannot_supply_own_independent_review(self):
        create = tool('delegate_task', assignee='lead', objective='撰写报告', deliverable_paths=['report.md'])
        self_review = tool('delegate_task', assignee='lead', objective='对自己验收', deliverable_paths=['report.md'],
                           review={'candidate':'lead','criteria':[{'id':'r','kind':'inspect','description':'check facts'}]})
        detail, _ = self.execute(('lead','reviewer'), [create, write('report.md', 'This report has one author.'),
            {'text':'Done.'}, self_review, self_review, {'text':'All work is done.'}], acceptance_files=['report.md'])
        self.assertEqual(detail['run']['status'], 'failed', detail['run'])
        self.assertEqual([e['recipient'] for e in detail['room_events'] if e['kind']=='assignment'], ['lead'])
        self.assertTrue(any('reviewer' in e.get('preview','') and e.get('ok') is False for e in detail['events']))

    def test_invalid_larger_room_recipient_gets_real_choices_then_bounded_failure(self):
        invalid = tool('delegate_task', assignee='chief', objective='写报告', deliverable_paths=['report.md'])
        detail, schemas = self.execute(('chief','writer','reviewer'),[invalid,invalid,{'text':'[BLOCKED] 无法分配'}])
        self.assertEqual(detail['run']['status'], 'failed')
        self.assertTrue(schemas and all(set(s)=={'writer','reviewer'} for s in schemas))
        rows = [e for e in detail['events'] if e['kind']=='tool_result' and e.get('operation')=='delegate_task']
        self.assertEqual(len(rows), 2)
        self.assertTrue(all('writer' in e['preview'] and 'reviewer' in e['preview'] for e in rows))

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
