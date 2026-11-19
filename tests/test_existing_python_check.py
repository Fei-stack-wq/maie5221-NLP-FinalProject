"""Execute real frozen Boolean checks, then cite the host's observed E receipt."""
import hashlib
import json
import re
import unittest
from copy import deepcopy
from unittest.mock import patch

from miniagent.agent import COORDINATOR_PROMPT
from miniagent.config import Limits, Settings
from miniagent.crew_progress import MemberProgress
from miniagent.rao import Observation, RAOState, StepRecord
from miniagent.sandbox import Sandbox
from miniagent.studio_reviews import Review, ReviewSession, Rule, digest
from miniagent.studio_service import StudioService
from miniagent.tools import ToolContext, build_default_registry
from tests.support import cleanup, make_temp_dir
from tests.test_signed_review_handoff import RoleProvider
from tests.test_studio_conversations import tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


PRODUCT = "delivery.json"
CHECK = "checks/verify.py"
RULES = [{"id": "identity", "method": "execute", "description": "Verify the exact delivered record identity."},
         {"id": "behavior", "method": "execute", "description": "Verify the delivered shipment is ready."}]
ADAPTER = ("import runpy\nchecks = runpy.run_path('checks/verify.py')\n"
           "assert checks['identity']() is True\nprint('PASS original identity')\n"
           "assert checks['behavior']() is True\nprint('PASS original behavior')\n")


def original_module(product):
    sha = hashlib.sha256(product.encode()).hexdigest()
    return (
        "import hashlib\nimport json\nimport sys\nfrom pathlib import Path\n"
        "def identity():\n"
        f"    answer = hashlib.sha256(Path({PRODUCT!r}).read_bytes()).hexdigest() == {sha!r}\n"
        "    print('IDENTITY_OK=' + str(answer))\n    return answer\n"
        "def behavior():\n"
        f"    answer = json.loads(Path({PRODUCT!r}).read_text())['ready'] is True\n"
        "    print('BEHAVIOR_OK=' + str(answer))\n    return answer\n"
        "if __name__ == '__main__':\n    sys.exit(0 if identity() and behavior() else 1)\n"
    )


class CatalogProvider(RoleProvider):
    def __init__(self, scripts):
        super().__init__(scripts)
        self.catalogs = []
        self.submitted_refs = []

    def chat(self, messages, tools=None, temperature=0):
        system = next(m['content'] for m in messages if m['role'] == 'system')
        match = re.search(r'你是「([^」]+)」', system)
        role = 'lead' if system == COORDINATOR_PROMPT else match[1] if match else None
        stream = self.providers[role]
        if stream.cursor < len(stream.script) and any(
                key in stream.script[stream.cursor] for key in ('_submit_catalog', '_run_catalog_template')):
            marker = stream.script[stream.cursor]
            catalog = None
            for message in reversed(messages):
                if message.get('role') != 'tool':
                    continue
                try:
                    value = json.loads(message.get('content', ''))
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict) and 'candidate_version' in value and 'evidence' in value:
                    catalog = value
                    break
            if catalog is None:
                raise AssertionError('The real list_review_evidence response is required')
            if marker.get('_run_catalog_template'):
                template = catalog['execute']['existing_python_check']
                code = template['code'].replace("['check_identity']", "['identity']").replace(
                    "['check_behavior']", "['behavior']")
                stream.script[stream.cursor] = tool(template['tool'], code=code)
                return super().chat(messages, tools, temperature)
            self.catalogs.append(deepcopy(catalog))
            wanted = marker['_submit_catalog']
            candidates = [row for row in catalog['evidence'] if row['kind'] == wanted]
            if not candidates:
                candidates = [row for row in catalog['evidence'] if row['kind'] in {'failed_check', 'read'}]
            receipt = candidates[-1]['ref']
            self.submitted_refs.append(receipt)
            submission = tool('submit_review', candidate_version=catalog['candidate_version'], results=[{
                'criterion_id': rule['id'], 'verdict': 'passed', 'evidence': [receipt],
                'reason': 'The original frozen function returned True in the observed invocation.'
            } for rule in RULES])
            if marker.get('change_script'):
                mutation = tool('run_python', code="from pathlib import Path\nPath('checks/verify.py').write_text('changed after checking')")
                submission['tool_calls'] = mutation['tool_calls'] + submission['tool_calls']
            stream.script[stream.cursor] = submission
        return super().chat(messages, tools, temperature)


class ExistingPythonCheckTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir('existing-python-check')
        self.service = StudioService(self.root / 'runs', Settings(api_key='test-only'))
        self.addCleanup(cleanup, self.root)
        self.addCleanup(self.service.close)

    def execute(self, *, ready=True, change_script=False, use_template=False, exit_behavior=False):
        product = json.dumps({'shipment': 'original', 'ready': ready})
        module = original_module(product)
        if exit_behavior:
            module = module.replace("    print('BEHAVIOR_OK=' + str(answer))\n    return answer\n",
                                    "    print('PASS unreturned behavior')\n    sys.exit(0)\n")
        provider = CatalogProvider({
            'lead': [tool('delegate_task', assignee='builder', objective='Publish the shipment and original check module.',
                          deliverable_paths=[PRODUCT, CHECK], remaining_roles=['inspector']),
                     tool('delegate_task', assignee='inspector', objective='Execute the existing checks independently.',
                          remaining_roles=[], review={'candidate': 'builder', 'criteria': RULES}),
                     {'text': 'Delivered the checked shipment.' if ready and not change_script and not exit_behavior else
                              '[BLOCKED] The required original checks or candidate version are unresolved.'}],
            'builder': [write(PRODUCT, product), write(CHECK, module), {'text': 'Published the immutable shipment and checks.'}],
            'inspector': [tool('read_file', path=PRODUCT), tool('run_shell', command='python3 ' + CHECK),
                          tool('list_review_evidence'), {'_submit_catalog': 'read'},
                          tool('read_file', path=CHECK), {'_run_catalog_template': True} if use_template else tool('run_python', code=ADAPTER),
                          tool('list_review_evidence'), {'_submit_catalog': 'passed_check', 'change_script': change_script},
                          {'text': '[BLOCKED] No usable execution receipt or unchanged candidate can be certified.'}],
        })
        with patch('miniagent.studio_service.DeepSeekProvider', return_value=provider):
            run = self.service.start(request('team', conversation=roster('lead', 'builder', 'inspector'),
                objective='Deliver an independently checked ready shipment.', acceptance_files=[PRODUCT, CHECK],
                limits={'max_steps': 90}))
            detail = finished(self.service, run['id'])
        reports = [e['handoff'] for e in detail['room_events'] if e['kind'] == 'member_report']
        trace = [json.loads(line) for line in (self.root / 'runs' / run['id'] / 'trace.jsonl').read_text().splitlines()]
        return detail, reports, provider, trace, product, module

    def test_existing_boolean_functions_gain_receipt_only_after_real_top_level_assertions(self):
        detail, reports, provider, trace, product, module = self.execute(use_template=True)
        self.assertEqual(detail['run']['status'], 'completed', [(r['role'], r['remaining_issue']) for r in reports])
        self.assertNotIn('passed_check', {e['kind'] for e in provider.catalogs[0]['evidence']})
        passed, = [e for e in provider.catalogs[1]['evidence'] if e['kind'] == 'passed_check']
        self.assertEqual(provider.submitted_refs[1], passed['ref'])
        reviewer = next(r for r in reports if r['role'] == 'inspector')
        assessment = reviewer['native_review']['assessment']
        self.assertEqual(assessment['verdict'], 'passed')
        self.assertEqual(reviewer['native_review']['files'][CHECK], hashlib.sha256(module.encode()).hexdigest())
        self.assertEqual(assessment['candidate_version'], digest({PRODUCT: hashlib.sha256(product.encode()).hexdigest(),
                                                                 CHECK: hashlib.sha256(module.encode()).hexdigest()}))
        submissions = [r for r in trace if r['kind'] == 'tool_result' and r['task_id'] == reviewer['task_id']
                       and r['tool'] == 'submit_review']
        self.assertEqual([r['ok'] for r in submissions], [False, True])
        self.assertIn('正面证据', submissions[0]['preview'])
        for finding in assessment['results']:
            receipt, = finding['receipts']
            self.assertEqual(receipt['ref'], passed['ref'])
            self.assertTrue(receipt['asserted'])
            actual, = [r for r in trace if r['kind'] == 'tool_result' and r['task_id'] == reviewer['task_id']
                       and r.get('tool_call_id') == receipt['tool_call_id']]
            self.assertEqual((actual['tool'], actual['exit_code']), ('run_python', 0))
        public = self.root / 'runs' / detail['run']['id'] / 'agents' / 'lead'
        self.assertEqual((public / CHECK).read_text(), module)
        self.assertEqual((public / PRODUCT).read_text(), product)
        self.assertIn('existing_python_check', provider.catalogs[0]['execute'])

    def test_catalog_template_rejects_function_exiting_zero_after_printing_pass(self):
        detail, reports, provider, trace, _, _ = self.execute(use_template=True, exit_behavior=True)
        self.assertNotEqual(detail['run']['status'], 'completed')
        self.assertFalse(any(e['kind'] == 'passed_check' for e in provider.catalogs[-1]['evidence']))
        reviewer = next(r for r in reports if r['role'] == 'inspector')
        self.assertIsNone(reviewer['native_review']['assessment'])
        invocation, = [r for r in trace if r['kind'] == 'tool_result' and r['task_id'] == reviewer['task_id']
                       and r['tool'] == 'run_python']
        self.assertEqual(invocation['exit_code'], 1)
        self.assertIn('PASS unreturned behavior', invocation['preview'])
        self.assertIn('in checked_call', invocation['preview'])

    def test_false_original_function_cannot_certify_even_when_an_earlier_assertion_passed(self):
        detail, reports, provider, trace, _, _ = self.execute(ready=False)
        self.assertNotEqual(detail['run']['status'], 'completed')
        self.assertFalse(any(e['kind'] == 'passed_check' for e in provider.catalogs[-1]['evidence']))
        reviewer = next(r for r in reports if r['role'] == 'inspector')
        self.assertIsNone(reviewer['native_review']['assessment'])
        invocation, = [r for r in trace if r['kind'] == 'tool_result' and r['task_id'] == reviewer['task_id']
                       and r['tool'] == 'run_python']
        self.assertEqual(invocation['exit_code'], 1)
        self.assertIn('IDENTITY_OK=True', invocation['preview'])
        self.assertIn('BEHAVIOR_OK=False', invocation['preview'])

    def test_changed_frozen_script_cannot_reuse_old_candidate_or_observed_pass(self):
        detail, reports, provider, trace, _, module = self.execute(change_script=True)
        self.assertNotEqual(detail['run']['status'], 'completed')
        self.assertTrue(any(e['kind'] == 'passed_check' for e in provider.catalogs[-1]['evidence']))
        reviewer = next(r for r in reports if r['role'] == 'inspector')
        self.assertIsNone(reviewer['native_review']['assessment'])
        self.assertFalse(reviewer['published'])
        attempts = [r for r in trace if r['kind'] == 'tool_result' and r['task_id'] == reviewer['task_id']
                    and r['tool'] == 'submit_review']
        self.assertIn('快照', attempts[-1]['preview'])
        public = self.root / 'runs' / detail['run']['id'] / 'agents' / 'lead' / CHECK
        self.assertEqual(public.read_text(), module)

    def test_direct_exit_and_prints_do_not_invent_a_passed_check(self):
        cases = {
            'print_only': "print('PASS claimed result')\n",
            'constant_return': "import sys\ndef check(): return True\nsys.exit(0 if check() else 1)\n",
            'unexecuted_branch': "import sys\ndef check(): raise AssertionError('not executed')\nsys.exit(0)\n",
            'hidden_failure': "import sys\ntry:\n    assert False, 'real failure'\nexcept AssertionError:\n    print('hidden failure')\nsys.exit(0)\n",
        }
        for name, source in cases.items():
            with self.subTest(name=name):
                directory = self.root / name
                directory.mkdir()
                (directory / 'check.py').write_text(source)
                progress = MemberProgress(directory)
                registry = build_default_registry()
                ctx = ToolContext(sandbox=Sandbox(directory), limits=Limits(tool_timeout_seconds=5))
                args = {'command': 'python3 check.py'}
                result = registry.dispatch('run_shell', args, ctx)
                self.assertTrue(result.ok, result.content)
                state = RAOState(goal='Observe a real direct exit.', task_id=name, history=[{
                    'role': 'assistant', 'tool_calls': [{'id': 'direct', 'function': {
                        'name': 'run_shell', 'arguments': json.dumps(args)}}]}])
                progress.observe(StepRecord(step=1, observations=[Observation(
                    step_id='direct', source='run_shell', tool_call_id='direct', ok=result.ok,
                    content=result.content, meta=result.meta)]), state)
                self.assertFalse(any(row['kind'] == 'passed_check' for row in progress.review_evidence().values()))

    def test_existing_callable_template_requires_an_available_python_tool(self):
        directory = self.root / 'guidance'
        directory.mkdir()
        for available in [('run_shell',), ('run_browser',), ('run_python', 'run_shell')]:
            with self.subTest(available=available):
                item = Review('target', 'builder', 'inspector', 'Run original checks.',
                              [Rule(**r) for r in RULES], {CHECK: 'original-script-digest'})
                session = ReviewSession(item, dict(item.files), MemberProgress(directory), 'attempt',
                                        available_tools=available)
                template = session.catalog()['execute'].get('existing_python_check')
                self.assertEqual(template is not None, 'run_python' in available)


if __name__ == '__main__':
    unittest.main()
