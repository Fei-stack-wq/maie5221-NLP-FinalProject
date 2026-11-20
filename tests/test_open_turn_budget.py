from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from miniagent.acceptance import accept_files
from miniagent.agent import Agent
from miniagent.config import Limits, Settings
from miniagent.providers import ScriptedProvider
from miniagent.rao import Budget
from miniagent.sandbox import Sandbox
from miniagent.studio_service import StudioService
from miniagent.tools import build_default_registry
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


class OpenTurnBudgetTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def execute(self, script, limits, **kwargs):
        budget = Budget.from_limits(limits)
        agent = Agent(name='worker', provider=ScriptedProvider(script), sandbox=Sandbox(self.root/'work'),
                      registry=build_default_registry(), settings=Settings(limits=limits))
        return agent.run('Carry out the work', budget=budget, **kwargs), budget

    def test_default_runs_past_100_rounds_and_400_actions_and_measures_actual_usage(self):
        script = [{'tool_calls':[{'name':'write_file','arguments':{'path':'work.md','content':f'Completed independent check {i*4+j}.'}}
                                 for j in range(4)]} for i in range(101)] + [{'text':'done'}]
        result, budget = self.execute(script, Limits(), acceptance=accept_files('work.md'))
        self.assertTrue(result.succeeded, result.error)
        self.assertEqual((budget.steps_used, budget.actions_used), (102, 404))
        self.assertEqual((budget.steps_left, budget.actions_left), (None, None))
        self.assertEqual((self.root/'work/work.md').read_text(), 'Completed independent check 403.')

    def test_explicit_cap_and_cancel_still_stop_while_repeat_warnings_allow_recovery(self):
        work = [write('work.md', str(i)) for i in range(10)]
        result, budget = self.execute(work, Limits(max_steps=3))
        self.assertEqual((result.status, budget.steps_used, budget.steps_left), ('limit_exceeded', 3, 0))
        cancelled = []
        budget = Budget.from_limits(Limits())
        agent = Agent(name='worker', provider=ScriptedProvider(work), sandbox=Sandbox(self.root/'work'),
                      registry=build_default_registry(), settings=Settings(), should_cancel=lambda: bool(cancelled))
        result = agent.run('Stop on request', budget=budget, on_step=lambda r,s: cancelled.append(True))
        self.assertEqual((result.status, budget.steps_used), ('aborted', 1))
        result, budget = self.execute([tool('list_dir')] * 12 + [write('result.md', 'real result'), {'text':'done'}],
                                      Limits(), acceptance=accept_files('result.md', min_bytes=1))
        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(budget.steps_used, 14)

    def test_team_accepts_seventeenth_substantive_handoff_and_preserves_unlimited_forks(self):
        script = []
        for i in range(17):
            script.extend([tool('delegate_task', assignee='writer', objective=f'Implement revision {i}', remaining_roles=['review']),
                           write('work.md', f'Independent work for revision {i}.'), {'text':f'Published revision {i}.'}])
        script.extend([tool('delegate_task', assignee='review', objective='Check the final revision', remaining_roles=[]),
                       tool('read_file', path='work.md'), {'text':'Reviewed revision 16.'}, {'text':'Delivered.'}])
        room = StudioService(self.root/'runs', Settings(api_key='offline'))
        self.addCleanup(room.close)
        with patch('miniagent.studio_service.DeepSeekProvider', return_value=RecordingProvider(script)):
            run = room.start(request('team', objective='Build successive revisions then review', conversation=roster('lead','writer','review'),
                                     limits={'max_steps':0}, acceptance_files=['work.md']))
            detail = finished(room, run['id'])
        self.assertEqual(detail['run']['status'], 'completed', detail['diagnostics'])
        assignments = [e for e in detail['room_events'] if e['kind']=='assignment']
        self.assertEqual(len(assignments),18)
        for e in assignments:
            self.assertIsNone(e['allocation']['steps_left'])
            self.assertIsNone(e['allocation']['actions_left'])
            self.assertIsNone(e['reserved']['seconds_left'])
            self.assertIsNone(e['allocation']['seconds_left'])
            for share in e['protected_peer_shares'].values():
                self.assertIsNone(share['seconds_left'])
        self.assertEqual(detail['run']['limits']['max_seconds'], 0)
        steps = [event for event in detail['events'] if event['kind'] == 'step_start']
        self.assertTrue(steps)
        self.assertTrue(all(event['seconds_remaining'] is None for event in steps))
        self.assertEqual(detail['run']['limits']['max_steps'],0)
        self.assertEqual(detail['diagnostics']['configuration']['max_steps'],0)
        child = Budget.from_limits(Limits(max_steps=0,max_total_tokens=600)).fork(.5)
        self.assertEqual((child.steps_left,child.actions_left,child.tokens_left), (None,None,300))
