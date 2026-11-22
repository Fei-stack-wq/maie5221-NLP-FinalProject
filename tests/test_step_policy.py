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

    def test_coordinator_replays_completed_turns_and_interleaved_steering(self):
        from threading import Lock
        from miniagent.studio_steering import RunSteering

        ops = frozenset({"list_dir", "ask_human"})
        browse = {"name": "list_dir", "arguments": {}}
        script = [
            {"text": "I will check both scopes.", "tool_calls": [browse, browse]},
            {"text": "<｜tool▁calls▁begin｜> never-ran"},
            {"tool_calls": [{"name": "ask_human", "arguments": {"question": "Which room?"}}]},
            *[{"text": f"Inspect {i}", "tool_calls": [browse]} for i in range(4)],
            {"text": "Done"},
        ]
        agent, provider = self.agent(script, human_handler=lambda _: "A101")
        inbox = RunSteering(Lock(), lambda *args, **kwargs: None)
        def observe(record, _state):
            if record.step == 1:
                inbox.rows.append({"request_id": "scope", "text": "Include the private handoff."})
        result = agent.run("Inspect scopes", step_policy=lambda *_: StepPolicy("coordinate", allowed_tools=ops, compact=True),
                           steering=inbox.subscribe("lead", "run", owner=True), on_step=observe)
        self.assertEqual(result.status, "unverified", result.error)
        repaired = provider.requests[2][0]
        self.assertNotIn("never-ran", str(repaired))
        first = next(i for i, message in enumerate(repaired) if message["role"] == "assistant")
        self.assertEqual(repaired[first]["content"], script[0]["text"])
        self.assertEqual([r["tool_call_id"] for r in repaired[first+1:first+3]], ["call_1_0", "call_1_1"])
        self.assertEqual(repaired[first+3]["content"], "Include the private handoff.")
        self.assertIn("工具协议纠正", str(repaired))
        # Recent answers stay paired with the real question. When the pair
        # rolls out, its authenticated answer and the steering survive once.
        after_question = provider.requests[3][0]
        self.assertEqual(sum("A101" in (m.get("content") or "") for m in after_question), 1)
        final = provider.requests[-1][0]
        self.assertEqual(sum("A101" in (m.get("content") or "") for m in final), 1)
        self.assertEqual(sum(m.get("content") == "Include the private handoff." for m in final), 1)
        self.assertNotIn(script[0]["text"], str(final))
        self.assertEqual([m["content"] for m in final if m["role"] == "assistant"], [f"Inspect {i}" for i in range(4)])
        for messages, offered in provider.requests:
            DeepSeekProvider._validate_history(messages, require_reasoning=False)
            self.assertEqual(set(offered), ops)

    def test_coordinator_preserves_thinking_with_its_original_tool_call(self):
        class ThinkingProvider(RecordingProvider):
            def chat(self, messages, tools=None, temperature=0.0):
                DeepSeekProvider._validate_history(messages, require_reasoning=True)
                answer = super().chat(messages, tools, temperature)
                return answer.model_copy(update={"reasoning_content": f"reasoning turn {self.cursor}"})

        script = [{"text": "Check the public room.",
                   "tool_calls": [{"name": "list_dir", "arguments": {}}]}, {"text": "Done"}]
        agent, _ = self.agent(script)
        provider = ThinkingProvider(script)
        agent.provider = agent.unit.provider = provider
        result = agent.run("Review public room", step_policy=lambda *_: StepPolicy(
            "coordinate", allowed_tools=frozenset({"list_dir"}), compact=True))
        self.assertEqual(result.status, "unverified", result.error)
        [first] = [row for row in provider.requests[1][0] if row["role"] == "assistant"]
        self.assertEqual(first["reasoning_content"], "reasoning turn 1")
        self.assertEqual(first["content"], "Check the public room.")

    def test_compact_work_keeps_assignment_and_authenticated_answer_then_can_write(self):
        operations = frozenset({"ask_human", "run_python", "write_file"})
        script = [
            {"tool_calls":[{"name":"ask_human","arguments":{"question":"Which room?"}}]},
            {"tool_calls":[{"name":"run_python","arguments":{"code":"print('transient tool text')"}}]},
            {"tool_calls":[{"name":"write_file","arguments":{"path":"room.md","content":"A101"}}]},
            {"text":"Done"}]
        agent, provider = self.agent(script, human_handler=lambda _: "A101")
        result = agent.run("Record the confirmed room", conversation_context="Own the booking document",
                           step_policy=lambda state, _: StepPolicy("work", allowed_tools=operations,
                                                                  compact=state.step_index == 2),
                           acceptance=accept_files("room.md", min_bytes=1))
        self.assertEqual(result.status, "completed", result.error)
        prompt, tools = provider.requests[2]
        contents = "\n".join(msg["content"] for msg in prompt)
        for fact in ("Own the booking document", "Record the confirmed room", "[用户已答复]", "Which room?", "A101"):
            self.assertIn(fact, contents)
        self.assertNotIn("transient tool text", contents)
        self.assertEqual(set(tools), operations)
        self.assertEqual((agent.sandbox.root / "room.md").read_text(), "A101")

    def test_rebased_work_tail_omits_failed_batches_and_can_take_another_checkpoint(self):
        def write(path):
            return {"tool_calls":[{"name":"write_file","arguments":{"path":path, "content":"persisted"}}]}

        pseudo = "<｜tool▁calls▁begin｜> never-executed-marker"
        agent, provider = self.agent([write("old.md"), write("first-tail.md"), {"text":pseudo},
                                      write("second-tail.md"), write("latest.md"), {"text":"Done"}])
        tools = frozenset({"write_file"})
        def policy(state, _):
            recovery = state.step_index in {1, 4}
            return StepPolicy("work", f"Checkpoint {state.step_index}" if recovery else "",
                              tools, compact=recovery)

        result = agent.run("Write the project notes", step_policy=policy,
                           acceptance=accept_files("latest.md", min_bytes=1))
        self.assertEqual(result.status, "completed", result.error)
        corrected = str(provider.requests[3][0])
        self.assertIn("Checkpoint 1", corrected)
        self.assertIn("first-tail.md", corrected)
        self.assertIn("工具协议纠正", corrected)
        self.assertNotIn("old.md", corrected)
        self.assertNotIn("never-executed-marker", corrected)
        final = str(provider.requests[-1][0])
        self.assertIn("Checkpoint 4", final)
        self.assertIn("latest.md", final)
        for old in ("Checkpoint 1", "old.md", "first-tail.md", "second-tail.md", "never-executed-marker"):
            self.assertNotIn(old, final)
        self.assertTrue(all(names == ["write_file"] for _, names in provider.requests))
        self.assertEqual(len(list(agent.sandbox.root.glob("*.md"))), 4)

    def test_semantic_compaction_after_work_recovery_summarizes_only_new_batches(self):
        from tests.test_semantic_compaction import summary
        old = [f"old-section-{i}.md" for i in range(8)]
        new = [f"new-section-{i}.md" for i in range(8)]
        script = [{"tool_calls":[{"name":"read_file", "arguments":{"path":name}}]} for name in old + new]
        agent, provider = self.agent([*script, {"text":"Reviewed"}])
        agent.settings.context_window_tokens = 18000
        for name in old + new:
            (agent.sandbox.root / name).write_text(name + "\n" + "x" * 4000)
        summaries = []
        def summarize(messages, **_):
            summaries.append((provider.cursor, str(messages)))
            return summary("old-summary-marker" if provider.cursor < len(old) else "new-summary-marker")
        provider.summarize_context = summarize
        result = agent.run("Review the project", step_policy=lambda state, _: StepPolicy(
            "work", "Active checkpoint", frozenset({"read_file"}), compact=state.step_index == len(old)))
        self.assertEqual(result.status, "unverified", result.error)
        self.assertTrue(any(turn < len(old) for turn, _ in summaries))
        current = [payload for turn, payload in summaries if turn >= len(old)]
        self.assertTrue(current)
        self.assertTrue(any("new-section-" in payload for payload in current))
        self.assertTrue(all("old-section-" not in payload and "old-summary-marker" not in payload for payload in current))
        for prompt, _ in provider.requests[len(old):]:
            self.assertNotIn("old-section-", str(prompt))
            self.assertNotIn("old-summary-marker", str(prompt))
        self.assertTrue(any("new-summary-marker" in str(messages) for messages, _ in provider.requests[len(old):]))

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

    def test_work_and_final_protocols_have_separate_single_corrections(self):
        pseudo = "<｜tool▁calls▁begin｜> run_shell"
        agent, provider = self.agent([{"text": pseudo},
                                      {"tool_calls": [{"name": "write_file", "arguments": {
                                          "path": "deliver.md", "content": "real artifact"}}]},
                                      {"text": pseudo}, {"text": "delivered"}])
        def policy(state, budget):
            if state.step_index < 2:
                return StepPolicy("work", allowed_tools=frozenset({"write_file"}))
            return StepPolicy("handoff", allowed_tools=frozenset(), compact=True)
        result = agent.run("write and report", step_policy=policy, budget=Budget(steps_left=4),
                           acceptance=accept_files("deliver.md", min_bytes=1))
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual([names for _, names in provider.requests], [["write_file"], ["write_file"], [], []])
        self.assertEqual(result.tool_calls, 1)

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

    def test_pure_text_handoff_cannot_retry_forever_without_tools_for_a_rejected_gate(self):
        agent, provider = self.agent([{"text": "complete"}, {"text": "still complete"}])
        final = lambda *_: StepPolicy("finalize", allowed_tools=frozenset(), compact=True)
        result = agent.run("deliver", step_policy=final, before_answer=lambda: "missing signed review")
        self.assertEqual((result.status, result.error), ("error", "missing signed review"))
        self.assertEqual([tools for _, tools in provider.requests], [[], []])

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
            # Even after excerpting both tools, the current goal, schemas and
            # framing cannot fit into the 600 tokens left for another work step.
            reserve = max(0, budget.tokens_left - 600) if state.step_index else 3000
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

    def test_repeated_questions_reuse_answer_and_leave_work_tools_available(self):
        ask = {"tool_calls":[{"name":"ask_human","arguments":{"decision_kind":"business_requirement","question":"when?"}}]}
        received = []
        agent, provider = self.agent([ask] * 8 + [{"text":"confirmed tomorrow"}],
                                     human_handler=lambda q: received.append(q) or "tomorrow")
        result = agent.run("confirm requirements")
        self.assertEqual(result.status, "unverified")
        self.assertEqual(result.steps, 9)
        self.assertIn("write_file", provider.requests[-1][1])
        self.assertEqual(received, ["when?"])
