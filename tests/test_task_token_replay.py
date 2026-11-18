"""Replay the 40 reported usage rows from the November 17 Studio budget incident."""
import tempfile
import unittest
from pathlib import Path

from miniagent.acceptance import accept_files
from miniagent.agent import Agent
from miniagent.config import Limits, Settings
from miniagent.providers import Provider
from miniagent.sandbox import Sandbox
from miniagent.schemas import LLMResponse, ToolCall, Usage
from miniagent.tools import build_default_registry


# Tokens only, from studio-20261117-233025-9ea412ddcfa4. Actions below are
# synthetic and local. No historical user prompt, tool data or key is replayed.
REPORTED_USAGE = (
    (1872, 42), (1931, 38), (2029, 71), (4633, 123), (6608, 75),
    (8468, 75), (10520, 75), (11895, 41), (12652, 553), (13268, 2331),
    (15662, 6703), (22427, 73), (22549, 274), (23056, 1532), (24759, 370),
    (25302, 228), (25724, 226), (26144, 222), (26560, 78), (27156, 285),
    (27635, 205), (28093, 285), (28631, 78), (29031, 338), (29622, 357),
    (30232, 405), (30890, 464), (31607, 541), (32401, 659), (33313, 777),
    (34343, 895), (35491, 1013), (36757, 1131), (38141, 1249), (39643, 1367),
    (41302, 92), (41815, 453), (42521, 76), (42898, 309), (43380, 279),
)


class RecordedUsage(Provider):
    def __init__(self, receipts):
        self.calls = 0
        self.receipts = receipts

    def chat(self, messages, tools=None, temperature=0.0):
        prompt, completion = REPORTED_USAGE[self.calls]
        self.calls += 1
        usage = Usage(prompt_tokens=prompt, completion_tokens=completion)
        if self.calls == len(REPORTED_USAGE):
            return LLMResponse(text="Receipts complete.", finish_reason="stop", usage=usage)
        path = f"step-{self.calls:02d}.txt"
        write = ToolCall(id=f"call-{self.calls}", name="write_file",
                         arguments={"path": path, "content": self.receipts[path]})
        return LLMResponse(tool_calls=[write], finish_reason="tool_calls", usage=usage)


class TaskTokenReplayTest(unittest.TestCase):
    def test_same_recorded_usage_with_unlimited_and_explicit_cap(self):
        receipts = {f"step-{i:02d}.txt": f"Locally verified harmless action {i}."
                    for i in range(1, len(REPORTED_USAGE))}
        self.assertEqual(sum(sum(u) for u in REPORTED_USAGE), 1_035_349)
        self.assertEqual(max(p for p, _ in REPORTED_USAGE), 43_380)
        for cap in (0, 1_000_000):
            with self.subTest(cap=cap), tempfile.TemporaryDirectory() as directory:
                provider = RecordedUsage(receipts)
                sandbox = Sandbox(Path(directory), agent_name="replay")
                agent = Agent("replay", provider, build_default_registry(), sandbox,
                              Settings(limits=Limits(max_total_tokens=cap)))
                result = agent.run("Write all 39 local receipt files and report completion.",
                                   acceptance=accept_files(*receipts, min_bytes=16))
                if cap == 0:
                    self.assertEqual((provider.calls, result.usage.total), (40, 1_035_349))
                    self.assertTrue(result.succeeded, result.error)
                    for name, content in receipts.items():
                        self.assertEqual((sandbox.root / name).read_text(), content)
                else:
                    self.assertEqual(result.status, "limit_exceeded")
                    self.assertIn("token 预算", result.error)
                    self.assertEqual((provider.calls, result.usage.total), (39, 991_690))
                    self.assertIsNone(result.acceptance_report)
