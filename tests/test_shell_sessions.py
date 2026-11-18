import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from miniagent.agent import Agent
from miniagent.config import Limits, Settings
from miniagent.member_processes import managed_member_processes
from miniagent.providers import ScriptedProvider
from miniagent.rao import Budget, StepPolicy
from miniagent.sandbox import Sandbox
from miniagent.shell_tool import command_timeout
from miniagent.tools import ToolContext, build_default_registry


@unittest.skipUnless(os.name == 'posix', 'requires POSIX processes')
class ShellSessionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.registry = build_default_registry()
        self.ctx = ToolContext(Sandbox(self.root), Limits())

    def run_tool(self, name, **args):
        return self.registry.dispatch(name, args, self.ctx)

    def test_default_explicit_host_and_deadline(self):
        self.assertEqual(command_timeout(self.ctx, None), 120)
        self.assertEqual(command_timeout(self.ctx, 301), 301)
        self.assertEqual(command_timeout(self.ctx, 9000), 600)
        self.ctx.limits.tool_timeout_seconds = 11
        self.assertEqual(command_timeout(self.ctx, 60), 11)
        for invalid in [0, -1, float('inf'), float('nan')]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                command_timeout(self.ctx, invalid)
        self.ctx.deadline = time.monotonic() + .15
        result = self.run_tool('run_shell', command='exec 1>&- 2>&-; sleep 4')
        self.assertFalse(result.ok)
        self.assertTrue(result.meta['timed_out'])
        self.assertLess(result.meta['elapsed_ms'], 1500)

    def test_cancel_while_foreground_command_has_closed_its_pipes(self):
        cancelled = threading.Event()
        timer = threading.Timer(.15, cancelled.set)
        timer.start()
        self.addCleanup(timer.cancel)
        self.ctx.should_cancel = cancelled.is_set
        result = self.run_tool('run_shell', command='exec 1>&- 2>&-; sleep 3')
        self.assertFalse(result.ok)
        self.assertTrue(result.meta['cancelled'])
        self.assertFalse(result.meta['timed_out'])
        self.assertLess(result.meta['elapsed_ms'], 1500)

    def test_owned_handle_return_once_with_nonzero_exit_and_preserved_output(self):
        command = 'echo start; sleep .15; echo end >&2; exit 7'
        with managed_member_processes(self.ctx.sandbox):
            launched = self.run_tool('run_shell', command=command, yield_after_seconds=0)
            self.assertEqual(launched.meta['process_state'], 'running')
            self.assertNotIn('exit_code', launched.meta)
            ref = launched.meta['process_id']
            other = ToolContext(Sandbox(self.root/'other'), Limits())
            with managed_member_processes(other.sandbox):
                self.assertFalse(self.registry.dispatch('poll_process', {'process_id':ref}, other).ok)
            final = self.run_tool('poll_process', process_id=ref, wait_seconds=2)
            self.assertFalse(final.ok)
            self.assertEqual((final.meta['exit_code'], final.meta['process_state'], final.meta['command']), (7, 'exited', command))
            self.assertIn('start', final.content)
            self.assertIn('end', final.content)
            self.assertFalse(self.run_tool('poll_process', process_id=ref).ok)

    def test_expired_command_and_cleanup_at_scope_exit(self):
        with managed_member_processes(self.ctx.sandbox) as cleanup:
            job = self.run_tool('run_shell', command='sleep 4', yield_after_seconds=0, timeout=.1)
            time.sleep(.22)
            final = self.run_tool('poll_process', process_id=job.meta['process_id'])
            self.assertEqual(final.meta['process_state'], 'timed_out')
            self.assertFalse(final.ok)
            self.run_tool('run_shell', command='sleep 3; touch leaked', yield_after_seconds=0)
        self.assertEqual(cleanup['uncollected_commands'], 1)
        self.assertFalse((self.root/'leaked').exists())
        self.assertIsNone(self.ctx.sandbox.command_sessions)

    def test_agent_protects_time_for_close_and_rejects_uncollected_exit(self):
        action = lambda name, **args: {'tool_calls':[{'name':name,'arguments':args}]}
        provider = ScriptedProvider([action('run_shell', command='sleep 3', timeout=30), {'text':'unfinished'}])
        agent = Agent(name='tester', provider=provider, registry=self.registry, sandbox=self.ctx.sandbox, settings=Settings())
        budget = Budget(steps_left=3, actions_left=4, seconds_left=1.2, tokens_left=None)
        records = []
        agent.run('inspect', budget=budget, step_policy=lambda s,b: StepPolicy('work', seconds_reserve=.8),
                  on_step=lambda r,s: records.append(r))
        self.assertTrue(records[0].observations[0].meta['timed_out'])
        self.assertLess(records[0].observations[0].meta['timeout_s'], .5)
        provider = ScriptedProvider([action('run_shell', command='sleep 4', yield_after_seconds=0),
                                     {'text':'done'}, {'text':'done'}])
        agent = Agent(name='tester', provider=provider, registry=self.registry, sandbox=self.ctx.sandbox, settings=Settings())
        result = agent.run('test')
        self.assertFalse(result.succeeded)
        self.assertIn('poll_process', result.error)
        self.assertIsNone(self.ctx.sandbox.command_sessions)
