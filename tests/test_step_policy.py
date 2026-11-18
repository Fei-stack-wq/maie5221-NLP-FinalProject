from pathlib import Path
import unittest
from unittest.mock import patch

from miniagent.acceptance import accept_files
from miniagent.agent import Agent
from miniagent.config import Settings
from miniagent.providers import DeepSeekProvider, ProviderError, ScriptedProvider
from miniagent.rao import Budget, ErrorClass, StepPolicy
from miniagent.sandbox import Sandbox
from miniagent.tools import build_default_registry
from tests.support import cleanup, make_temp_dir


class RecordingProvider(ScriptedProvider):
    def __init__(self, script):
        super().__init__(script)
        self.requests = []

    def chat(self, messages, tools=None, temperature=0.0):
        self.requests.append((messages, [spec.name for spec in tools or []]))
        return super().chat(messages, tools, temperature)


class StepPolicyTest(unittest.TestCase):
    def agent(self, script, **kwargs):
        root = make_temp_dir("policy")
        self.addCleanup(cleanup, root)
        provider = RecordingProvider(script)
        agent = Agent("test", provider, build_default_registry(), Sandbox(root),
                      Settings(), **kwargs)
        return agent, provider

    def test_compact_handoff_has_user_reply_but_no_source_or_stale_background(self):
        script = [
            {"tool_calls": [{"name": "ask_human", "arguments": {"question": "选哪间？"}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "app.md", "content": "raw-source-marker"}}]},
            {"text": "已交接 A101 / app.md"},
        ]
        agent, provider = self.agent(script, human_handler=lambda _: "A101")
        budget = Budget(steps_left=3, actions_left=1, tokens_left=100_000, seconds_left=100)
        def select(state, _budget):
            if state.step_index == 2:
                return StepPolicy("handoff", "交付工件 app.md", frozenset(), compact=True)
            return StepPolicy("work", allowed_tools=frozenset({"ask_human", "write_file"}))
        result = agent.run("预约系统", conversation_context="old-background-marker",
                           step_policy=select, budget=budget, acceptance=accept_files("app.md", min_bytes=1))
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.answer, script[-1]["text"])
        messages, tools = provider.requests[-1]
        self.assertEqual(tools, [])
        for marker in ["raw-source-marker", "old-background-marker"]:
            self.assertNotIn(marker, str(messages))
        for marker in ["A101", "选哪间", "交付工件", "预约系统"]:
            self.assertIn(marker, str(messages))
        self.assertEqual(set(provider.requests[0][1]), {"ask_human", "write_file"})

    def test_provider_tool_markup_is_never_run_and_corrected_structured_call_runs_once(self):
        pseudo = ('我先核实工作区。\n\n<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="run_shell">\n'
                  '<｜｜DSML｜｜ parameter name="command" string="true">touch ghost.txt</｜｜DSML｜｜ parameter>\n'
                  '</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>')
        actual = {"name":"run_python","arguments":{"code":"with open('log.txt','a') as f: f.write('once\\n')"}}
        agent,provider = self.agent([{"text":pseudo},{"tool_calls":[actual]},{"text":"Checked log."}])
        observed = []
        result = agent.run("Write a log", acceptance=accept_files("log.txt",min_bytes=1),
                           on_step=lambda row,_: observed.append(row))
        self.assertEqual(result.status, "completed", result.error)
        self.assertTrue(observed[0].unstructured_tool_call)
        self.assertEqual(observed[0].error_class, ErrorClass.T_PROTOCOL)
        self.assertFalse((agent.sandbox.root/"ghost.txt").exists())
        self.assertEqual((agent.sandbox.root/"log.txt").read_text(),"once\n")
        self.assertIn("工具协议纠正", str(provider.requests[1][0]))
        self.assertNotIn("ghost.txt", str(provider.requests[1][0]))
        self.assertNotIn("工具协议纠正", str(provider.requests[2][0]))

    def test_compact_handoff_corrects_legacy_markup_once_before_acceptance(self):
        pseudo = "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>run_shell"
        for phase,corrected in [("handoff",True),("finalize",False)]:
            with self.subTest(phase=phase):
                agent, provider = self.agent([{"text":pseudo},{"text":"真正的文字结论" if corrected else pseudo}])
                (agent.sandbox.root/"existing.md").write_text("prior checked artifact")
                result = agent.run("Summarize", step_policy=lambda *_: StepPolicy(phase,allowed_tools=frozenset(),compact=True),
                                   acceptance=accept_files("existing.md",min_bytes=1))
                self.assertEqual(result.status, "completed" if corrected else "error", result.error)
                self.assertEqual(len(provider.requests),2)
                self.assertEqual(provider.requests[1][1],[])
                self.assertIn("工具协议纠正", str(provider.requests[1][0]))
                self.assertNotIn(pseudo, str(provider.requests[1][0]))
                if not corrected:
                    self.assertNotEqual(getattr(result.acceptance_report,"verdict",None),"passed")
                    self.assertIn("纠正后仍",result.error)

    def test_literal_fenced_protocol_example_is_a_valid_text_response(self):
        example = "协议文档的示例：\n~~~~text\n<｜｜DSML｜｜ calls>\n~~~\n<｜tool▁calls▁begin｜>\n~~~~"
        agent, provider = self.agent([{"text":example}])
        result = agent.run("Explain a marker")
        self.assertEqual(result.answer, example)
        self.assertEqual(len(provider.requests),1)

    def test_navigation_paths_are_evidence_and_only_writes_emit_artifacts(self):
        def call(name, **arguments):
            return {"tool_calls": [{"name": name, "arguments": arguments}]}
        agent, _ = self.agent([call("write_file", path="app.py", content="pass"),
                               call("list_dir"), call("read_lines", path="app.py"),
                               call("search_files", query="pass"), {"text": "inspected"}])
        seen = []
        agent.run("inspect", on_step=lambda rec, _: seen.extend((o.source, o.artifacts) for o in rec.observations))
        self.assertIn(("write_file", ["app.py"]), seen)
        for name in ["list_dir", "read_lines", "search_files"]:
            self.assertIn((name, []), seen)

    def test_unoffered_control_rejects_whole_batch_and_text_only_is_bounded(self):
        writes = {"name": "write_file", "arguments": {"path": "oops.md", "content": "should not run"}}
        control = {"name": "delegate_task", "arguments": {"objective": "should not run"}}
        calls = []
        agent, provider = self.agent([{"tool_calls": [writes, control]}] * 4,
                                     delegate_handler=lambda payload: calls.append(payload))
        def policy(state, _budget):
            if state.step_index == 0:
                return StepPolicy("work", allowed_tools=frozenset({"write_file"}))
            return StepPolicy("final", allowed_tools=frozenset(), compact=True)
        records = []
        result = agent.run("stop", step_policy=policy, on_step=lambda record, _: records.append(record))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.steps, 3)
        self.assertEqual(calls, [])
        self.assertFalse((Path(result.sandbox) / "oops.md").exists())
        for record in records:
            self.assertEqual(len(record.observations), 2)
            self.assertTrue(all(obs.error_class == ErrorClass.A_SKIPPED for obs in record.observations))
        self.assertEqual([tools for _, tools in provider.requests], [["write_file"], [], []])

    def test_gate_and_required_check_survive_final_mode(self):
        final = lambda *_: StepPolicy("final", allowed_tools=frozenset(), compact=True)
        agent, provider = self.agent([{"text": "finished"}] * 3)
        gate = iter(["missing reviewer receipt", None, None])
        result = agent.run("deliver", step_policy=final, before_answer=lambda: next(gate),
                           acceptance=accept_files("missing.md", min_bytes=1))
        self.assertEqual(result.status, "failed_acceptance", result.error)
        self.assertIn("missing reviewer receipt", str(provider.requests[1][0]))
        self.assertIn("验收未通过", str(provider.requests[2][0]))
        self.assertEqual(result.acceptance_report.verdict, "failed")

    def test_blocked_report_is_not_success_even_when_no_checks_configured(self):
        agent, _ = self.agent([{"text": "[BLOCKED] missing input DB"}])
        result = agent.run("review", step_policy=lambda *_: StepPolicy("work"))
        self.assertEqual(result.status, "error")
        self.assertIn("missing input DB", result.error)

    def test_deliver_phase_only_mutates_the_named_outstanding_file(self):
        def write(path):
            return {"name":"write_file", "arguments":{"path": path, "content":"deliverable"}}
        agent, provider = self.agent([
            {"tool_calls": [write("docs/guide.md"), write("app.py")]},
            {"tool_calls": [{"name":"run_python", "arguments":{"code":"print(1)"}}]},
            {"tool_calls": [write("./docs/guide.md")]},
            {"text":"handed off"},
        ])
        root = agent.sandbox.root
        (root / "app.py").write_text("original source")
        paths = frozenset({"docs/guide.md"})
        captured = []
        def observe(record, _state):
            captured.append(((root / "docs/guide.md").exists(), record.ok))
        result = agent.run(
            "deliver guide", step_policy=lambda *_: StepPolicy("deliver", allowed_write_paths=paths),
            on_step=observe, acceptance=accept_files("docs/guide.md", min_bytes=1),
        )
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(captured, [(False, False), (False, False), (True, True), (True, True)])
        self.assertEqual((root / "app.py").read_text(), "original source")
        self.assertEqual(set(provider.requests[0][1]), {"edit_file", "append_file", "write_file"})
        for tool in agent.unit.tool_specs(allowed_write_paths=paths):
            self.assertEqual(tool.parameters["properties"]["path"]["enum"], ["docs/guide.md"])
        self.assertFalse(any("enum" in t.parameters["properties"].get("path",{})
                             for t in agent.unit.tool_specs()))

    def test_call_preflight_cannot_spend_reserved_tokens(self):
        agent, provider = self.agent([{"text": "unused"}])
        result = agent.run("deliver", budget=Budget(tokens_left=9000),
                           step_policy=lambda *_: StepPolicy("coordinate", token_reserve=8999))
        self.assertEqual(result.status, "limit_exceeded")
        self.assertEqual(provider.requests, [])

    def test_expensive_recent_tool_results_use_reserved_handoff_without_certifying_partial_work(self):
        reads = [{"name":"read_file","arguments":{"path":"source.md"}} for _ in range(2)]
        agent, provider = self.agent([{"tool_calls":reads},{"text":"[BLOCKED] Work still needs a repair."}])
        (agent.sandbox.root/"source.md").write_text("读" * 5000)
        fallback_calls = []
        def work(state,budget):
            reserve = max(0,budget.tokens_left - 18352) if state.step_index else 3000
            return StepPolicy("work", allowed_tools=frozenset({"read_file"}), token_reserve=reserve)
        def handoff(state,budget):
            fallback_calls.append((state.step_index,budget.tokens_left))
            return StepPolicy("handoff","Report the remaining issue.",frozenset(),compact=True)
        result = agent.run("Inspect source",budget=Budget(steps_left=3,tokens_left=80000),
                           step_policy=work,preflight_handoff=handoff)
        self.assertEqual(result.status,"error")
        self.assertIn("Work still needs",result.error)
        self.assertEqual(len(fallback_calls),1)
        self.assertEqual(len(provider.requests),2)
        self.assertEqual(provider.requests[1][1],[])
        self.assertNotIn("读"*50,str(provider.requests[1][0]))

    def test_human_gate_distinguishes_runtime_refusal_from_real_reply(self):
        blocked = {"name": "ask_human", "arguments": {"question": "who builds?", "decision_kind": "internal_routing"}}
        genuine = {"name": "ask_human", "arguments": {"question": "opening hours?"}}
        received = []
        agent, provider = self.agent(
            [{"tool_calls": [blocked]}, {"tool_calls": [genuine]}, {"text": "done"}],
            human_request_gate=lambda args: "choose the role locally" if args.get("decision_kind")=="internal_routing" else None,
            human_handler=lambda question: received.append(question) or "8–17",
        )
        observations = []
        agent.run("review", step_policy=lambda *_: StepPolicy("work", compact=True),
                  on_step=lambda record, _: observations.extend(record.observations))
        self.assertEqual(received, ["opening hours?"])
        self.assertEqual([obs.source for obs in observations], ["human_gate", "human"])
        self.assertNotIn("who builds?", str(provider.requests[-1]))
        self.assertIn("8–17", str(provider.requests[-1]))

    def test_transport_deadline_and_max_output_respect_dispatch_reserves(self):
        agent, _ = self.agent([])
        agent.provider = agent.unit.provider = DeepSeekProvider("fixture-key")
        budget = Budget(steps_left=4, tokens_left=20000, seconds_left=20)
        with patch("miniagent.providers.post_json", side_effect=ProviderError("fixture")) as post:
            agent.run("dispatch", budget=budget, step_policy=lambda *_: StepPolicy(
                "coordinate", allowed_tools=frozenset({"delegate_task"}), compact=True,
                token_reserve=8000, seconds_reserve=7))
        self.assertGreater(post.call_args.kwargs["deadline_seconds"], 0)
        self.assertLessEqual(post.call_args.kwargs["deadline_seconds"], 13)
        self.assertLess(post.call_args.args[1]["max_tokens"], 12000)

    def test_exact_duplicate_preference_has_one_human_reply_and_task_can_continue(self):
        call = {"name":"ask_human","arguments":{"question":"How many rooms?", "decision_kind":"user_preference"}}
        received = []
        write = {"name":"write_file","arguments":{"path":"rooms.md","content":"confirmed"}}
        agent, provider = self.agent([{"tool_calls":[call]}, {"tool_calls":[call, write]}, {"text":"delivered"}],
                                    human_handler=lambda q: received.append(q) or "four rooms")
        records = []
        result = agent.run("build", step_policy=lambda *_: StepPolicy("work", compact=True),
                           on_step=lambda rec, _: records.append(rec), acceptance=accept_files("rooms.md", min_bytes=1))
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(received, ["How many rooms?"])
        skipped = records[1].observations[0]
        self.assertEqual(skipped.source, "human_gate")
        self.assertEqual(skipped.meta["answered_action"], records[0].observations[0].action_id)
        self.assertEqual(str(provider.requests[-1][0]).count("four rooms"), 1)
        self.assertNotIn("four rooms", skipped.content)

    def test_changed_or_permission_question_and_new_attempt_each_get_a_new_reply(self):
        def ask(question, kind):
            return {"tool_calls":[{"name":"ask_human","arguments":{"question":question,"decision_kind":kind}}]}

        first = ask("opening time", "business_requirement")
        changed = ask("closing time", "business_requirement")
        approval = ask("approve operating this machine?", "external_permission")
        agent, _ = self.agent([first, first, changed, approval, approval, {"text":"done"},
                               first, {"text":"done again"}])
        answers = iter([None, "08:00", "17:00", "yes this time", "no this time", "changed to 09:00"])
        observed = []
        agent.unit.human_handler = lambda q: observed.append(q) or next(answers)
        agent.run("first")
        agent.run("new attempt")
        self.assertEqual(observed, ["opening time","opening time","closing time","approve operating this machine?",
                                    "approve operating this machine?","opening time"])

    def test_repeated_duplicate_only_turns_stop_without_more_human_requests(self):
        ask = {"tool_calls":[{"name":"ask_human","arguments":{"decision_kind":"business_requirement","question":"when?"}}]}
        received = []
        agent, provider = self.agent([ask] * 8, human_handler=lambda q: received.append(q) or "tomorrow")
        result = agent.run("confirm requirements")
        self.assertEqual(result.steps, 3)
        self.assertIn(ErrorClass.T_LOOP, result.error)
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(received, ["when?"])
