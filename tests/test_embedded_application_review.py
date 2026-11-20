"""Keep skipped calls out of signed checks and bind real tests of embedded applications."""
import hashlib
import importlib.util
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from miniagent.config import Limits
from miniagent.crew_progress import MemberProgress
from miniagent.providers import ScriptedProvider
from miniagent.rao import Budget, ErrorClass, RAOState, RAOUnit
from miniagent.sandbox import Sandbox
from miniagent.studio_crew import RoomCrew
from miniagent.studio_reviews import Review, ReviewSession, Rule
from miniagent.tools import build_default_registry


class ApplicationReviewTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.document = "<!doctype html><html><body><h1>Appointments</h1></body></html>"
        self.source = "HTML = " + repr(self.document) + "\ndef capacity():\n    return 4\n"
        self.source += "if __name__ == '__main__':\n    print('Service ready')\n"
        (self.root / "service.py").write_text(self.source)
        self.progress = MemberProgress(self.root)
        files = self.progress.project_versions()
        self.product = Review("candidate", "author", "qa", "Check the service", [
            Rule(id="capacity",method="execute",description="The service accepts four participants.")], files)
        self.session = ReviewSession(self.product, files, self.progress, "attempt-qa", reviewer_task="task-qa")
        self.provider = ScriptedProvider([])
        self.state = RAOState(goal="Check the service",task_id="task-qa",history=[{"role":"user","content":"Check."}])
        self.budget = Budget(tokens_left=200000,seconds_left=90,steps_left=30,actions_left=40)

    def step(self, name, code, allowed=None):
        self.provider.script.append({"tool_calls":[{"name":name,"arguments":{"code":code}}]})
        unit = RAOUnit(name="qa",provider=self.provider,registry=build_default_registry(),sandbox=Sandbox(self.root),limits=Limits())
        record = unit.step(self.state,self.budget,allowed_tools=allowed)
        self.progress.observe(record,self.state)
        self.session.observe(record)
        return record.observations[0]

    def test_unexecuted_call_after_signature_preserves_two_individually_cited_failures(self):
        self.product.criteria.append(Rule(id="startup",method="execute",description="The entry point starts."))
        bad = "import runpy\napi=runpy.run_path('service.py')\nassert api['capacity']() == 3"
        second = ("import subprocess,sys\nproc=subprocess.run([sys.executable,'service.py'],capture_output=True)"
                  "\nassert proc.stdout.strip() == b'Wrong banner'")
        failed_capacity = self.step("run_python",bad)
        failed_startup = self.step("run_python",second)
        self.assertFalse(failed_capacity.ok)
        self.assertFalse(failed_startup.ok)
        good = self.step("run_python",bad.replace("== 3","== 4") + "\nprint('PASS actual service capacity')")
        started = self.step("run_python",second.replace("Wrong banner","Service ready") + "\nprint('PASS actual service startup')")
        self.assertTrue(good.ok,good.content)
        self.assertTrue(started.ok,started.content)
        receipts = {v["tool_call_id"]:(key,v) for key,v in self.progress.review_evidence().items()}
        for observation in (good,started):
            self.assertEqual(receipts[observation.tool_call_id][1]["asserted_inputs"],self.product.files)
        self.assertEqual(len(self.progress.failed_checks()),2)
        incomplete = {"candidate_version":self.product.version,"results":[
            {"criterion_id":rule,"verdict":"passed","reason":"Independently checked the running frozen service.",
             "evidence":[receipts[observed.tool_call_id][0]]}
            for rule,observed in (("capacity",good),("startup",started))]}
        rejected = self.session.submit(None,incomplete)
        self.assertFalse(rejected.ok)
        self.assertIn("未处理",rejected.content)
        for finding,failed in zip(incomplete["results"],(failed_capacity,failed_startup)):
            finding["evidence"].append(receipts[failed.tool_call_id][0])
        self.assertTrue(self.session.submit(None,incomplete).ok)
        skipped = self.step("run_browser","assert page.title() == 'Guess'",allowed=frozenset({"read_file"}))
        self.assertEqual(skipped.error_class,ErrorClass.A_SKIPPED)
        self.assertFalse(self.progress.evidence()["undisclosed_failure_probes"])
        self.assertEqual(len(self.progress.failed_checks()),2)
        self.assertEqual(set(RoomCrew._bound_probe_conflicts(self.product,self.session,self.progress)),
                         set(self.progress.failed_checks()))
        self.assertEqual(self.session.assessment["verdict"],"passed")

        crew = SimpleNamespace(lead="lead",plan_order=["peer"],profiles=["author","qa","peer","lead"],
            delivery={"files":["unrelated.md"]},reviewer_probe_reviews={},run_id="run",record_event=Mock(),
            _transferable_probe_support=lambda *_: [])
        draft = {"checkpoint":self.root,"manifest":self.product.files,"base":self.product.files,
                 "inline_failures":self.progress.inline_failures(),"failed_checks":self.progress.failed_checks()}
        report = {"task_id":"task-qa"}
        self.assertTrue(RoomCrew._queue_reviewer_probes(crew,self.product,report,draft,self.session.assessment,[]))
        child = crew.reviewer_probe_reviews["task-qa"]["review"]
        self.assertEqual(child.reviewer,"peer")
        scopes = {key.removeprefix("fail:"):name for name,obs in (("startup",failed_startup),("capacity",failed_capacity))
                  for key in [receipts[obs.tool_call_id][0]]}
        for rule in child.criteria:
            business = scopes[rule.id.removeprefix("probe_")]
            self.assertIn('"id":"' + business + '"',rule.description)
            self.assertNotIn('"id":"' + ({"startup","capacity"}-{business}).pop() + '"',rule.description)

    @unittest.skipUnless(os.name=="posix" and importlib.util.find_spec("playwright"),"local Playwright required")
    def test_uncaught_locator_timeout_and_corrected_assertion_bind_the_same_embedded_document(self):
        document = self.document
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(document.encode())
            def log_message(self,*args):
                pass
        with ThreadingHTTPServer(("127.0.0.1",0),Handler) as server:
            serving = threading.Thread(target=server.serve_forever,daemon=True)
            serving.start()
            try:
                start = f"page.goto('http://127.0.0.1:{server.server_port}/')\n"
                wrong = self.step("run_browser",start+
                    "value=page.locator('#guessed').inner_text(timeout=140)\nassert value == 'Appointments'")
                if any(msg in wrong.content for msg in ("环境缺少 Chromium/Chrome","环境缺少 Playwright")):
                    self.skipTest("local Chromium required")
                right = self.step("run_browser",start+"expect(page.get_by_role('heading')).to_have_text('Appointments')")
            finally:
                server.shutdown()
                serving.join()
        self.assertFalse(wrong.ok)
        self.assertTrue(right.ok,right.content)
        records=self.progress.review_evidence()
        self.assertEqual({r["kind"] for r in records.values()},{"failed_check","passed_check"})
        self.assertTrue(all(r["browser_inputs"]==self.product.files for r in records.values()))

    def test_port_or_unrelated_and_ambiguous_document_cannot_identify_a_python_product(self):
        from miniagent.crew_progress import _observed_browser_files, _asserted_inputs
        served=hashlib.sha256(self.document.encode()).hexdigest()
        self.assertEqual(_observed_browser_files({},self.product.files,self.progress._embedded_html),set())
        self.assertEqual(_observed_browser_files({"asserted_browser_documents":["0"*64]},
            self.product.files,self.progress._embedded_html),set())
        (self.root/"other.py").write_text("OTHER = " + repr(self.document))
        current=self.progress.project_versions()
        self.assertEqual(_observed_browser_files({"asserted_browser_documents":[served]},current,
            self.progress._embedded_html),set())
        self.assertFalse(_asserted_inputs("import runpy\napi=runpy.run_path('service.py')\nassert 1+1==2"))
        self.assertFalse(_asserted_inputs("import subprocess,sys\nr=subprocess.run([sys.executable,'service.py'],cwd='/tmp')\nassert b'ok' in r.stdout"))
        self.assertFalse(_asserted_inputs("import subprocess,sys\nr=subprocess.run([sys.executable,'service.py'])\nassert r.returncode==0"))
