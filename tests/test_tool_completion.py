"""Host completion skips a model final turn without skipping runtime gates."""
import unittest
from unittest.mock import Mock

from miniagent.acceptance import accept_files
from miniagent.agent import Agent
from miniagent.config import Settings
from miniagent.rao import ToolCompletion
from miniagent.sandbox import Sandbox
from miniagent.tools import build_default_registry
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, tool


class ToolCompletionTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("tool-completion")
        self.addCleanup(cleanup, self.root)

    def agent(self, provider, **kwargs):
        return Agent(name="worker", provider=provider, registry=build_default_registry(),
                     sandbox=Sandbox(self.root, agent_name="worker"), settings=Settings(), **kwargs)

    @staticmethod
    def complete(record):
        return ToolCompletion("Host handoff from observed tools.",
                              {"calls": [obs.tool_call_id for obs in record.observations]})

    def test_completion_observes_the_whole_batch_and_still_runs_publication_and_acceptance(self):
        batch = {"tool_calls": [
            tool("write_file", path="delivery.txt", content="confirmed")["tool_calls"][0],
            tool("read_file", path="delivery.txt")["tool_calls"][0],
        ]}
        provider = RecordingProvider([batch])
        events = []

        def observed(record, state):
            self.assertEqual([obs.source for obs in record.observations], ["write_file", "read_file"])
            events.append("observed")

        def completion(record):
            self.assertEqual(events, ["observed"])
            events.append("completion")
            return self.complete(record)

        def publication():
            events.append("publication")

        result = self.agent(provider).run("Deliver a checked label.", on_step=observed,
            tool_completion=completion, before_answer=publication,
            acceptance=accept_files("delivery.txt", min_bytes=1, contains={"delivery.txt": "confirmed"}))
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.answer, "Host handoff from observed tools.")
        self.assertEqual((provider.cursor, result.steps, result.tool_calls), (1, 1, 2))
        self.assertEqual(events, ["observed", "completion", "publication"])

    def test_completion_cannot_override_a_publication_guard(self):
        provider = RecordingProvider([
            tool("write_file", path="delivery.txt", content="confirmed"),
            {"text": "The signed result should be published."},
        ])
        guard = Mock(return_value="The public baseline changed; re-review is required.")
        result = self.agent(provider).run("Deliver a checked label.",
            tool_completion=self.complete, before_answer=guard,
            acceptance=accept_files("delivery.txt", min_bytes=1))
        self.assertEqual(result.status, "error")
        self.assertIn("public baseline changed", result.error)
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(provider.cursor, 2)
        self.assertIn("public baseline changed", str(provider.requests[-1][0]))

    def test_completion_cannot_override_a_failed_hard_acceptance_check(self):
        provider = RecordingProvider([tool("write_file", path="delivery.txt", content="wrong")])
        result = self.agent(provider, max_acceptance_retries=0).run("Deliver the confirmed label.",
            tool_completion=self.complete,
            acceptance=accept_files("delivery.txt", min_bytes=1, contains={"delivery.txt": "confirmed"}))
        self.assertEqual(result.status, "failed_acceptance", result.error)
        self.assertEqual(provider.cursor, 1)
        self.assertEqual((self.root / "delivery.txt").read_text(), "wrong")

    def test_failed_acceptance_keeps_the_existing_repair_flow_before_host_completion(self):
        provider = RecordingProvider([
            tool("write_file", path="delivery.txt", content="wrong"),
            tool("write_file", path="delivery.txt", content="confirmed"),
        ])
        result = self.agent(provider).run("Deliver the confirmed label.",
            tool_completion=self.complete,
            acceptance=accept_files("delivery.txt", min_bytes=1, contains={"delivery.txt": "confirmed"}))
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual((provider.cursor, result.steps), (2, 2))
        self.assertIn("验收未通过", str(provider.requests[-1][0]))
        self.assertEqual((self.root / "delivery.txt").read_text(), "confirmed")

    def test_cancellation_after_observation_prevents_host_completion(self):
        provider = RecordingProvider([tool("write_file", path="delivery.txt", content="confirmed")])
        cancelled = False

        def observed(record, state):
            nonlocal cancelled
            cancelled = True

        completion = Mock(side_effect=AssertionError("Cancelled work must not reach host completion."))
        result = self.agent(provider, should_cancel=lambda: cancelled).run("Deliver a checked label.",
            on_step=observed, tool_completion=completion,
            acceptance=accept_files("delivery.txt", min_bytes=1))
        self.assertEqual(result.status, "aborted", result.error)
        completion.assert_not_called()
        self.assertEqual(provider.cursor, 1)


if __name__ == "__main__":
    unittest.main()
