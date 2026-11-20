"""Acceptance of the final manuscript is independent of earlier research notes."""
import hashlib
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from miniagent.config import Settings
from miniagent.research import Page
from miniagent.studio_reviews import Review, ReviewQueue, ReviewSession, Rule, digest
from miniagent.studio_service import StudioError, StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


FINAL, NOTES = "final.md", "notes.md"
ARTICLE = "This shipment goes to Main Street. Independently sourced final article."
NOTE = "Working notes for the shipment, including the approved destination."
URL = "https://example.org/dispatch"
RULE = {"id": "claim", "description": "Verify the destination against the final dispatch order.", "method": "source"}


def hashed(text):
    return hashlib.sha256(text.encode()).hexdigest()


def submit(manifest, path):
    return tool("submit_review", candidate_version=digest(manifest), results=[
        {"criterion_id": "claim", "verdict": "passed", "evidence": ["file:" + path, "source:" + URL],
         "reason": "Compared the cited destination against the signed dispatch order."}])


class CurrentCandidateProvider(RecordingProvider):
    def chat(self, messages, tools=None, temperature=0):
        if self.cursor < len(self.script):
            for call in self.script[self.cursor].get("tool_calls", []):
                if call["name"] == "submit_review":
                    # The author's freshly archived external source is
                    # also versioned. Read the real candidate ID shown to the reviewer.
                    versions = re.findall(r'"candidate_version":"([0-9a-f]{64})"',
                                          "\n".join(m.get("content") or "" for m in messages))
                    call["arguments"]["candidate_version"] = versions[-1]
        return super().chat(messages, tools, temperature)


class RequiredSourceReviewTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("required-sources")
        self.service = StudioService(self.root / "runs", Settings(api_key="test-credential"))
        self.addCleanup(cleanup, self.root)
        self.addCleanup(self.service.close)

    def execute(self, steps, *, members=("lead", "writer", "checker"), provider_type=RecordingProvider,
                final_path=FINAL, objective="Publish the sourced dispatch article.", **kwargs):
        provider = provider_type(steps)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider), \
             patch("miniagent.research.fetch", return_value=Page(URL, "text/plain", ARTICLE.encode(), False)):
            job = self.service.start(request("team", conversation=roster(*members),
                objective=objective, required_source_review_paths=[final_path],
                limits={"max_steps": 90}, **kwargs))
            return finished(self.service, job["id"]), provider

    def test_final_file_requires_fresh_source_and_read_of_actual_manuscript(self):
        files = {FINAL: hashed(ARTICLE), NOTES: hashed(NOTE)}
        review = {"candidate": "writer", "criteria": [RULE]}
        steps = [tool("delegate_task", assignee="writer", objective="Write final article and notes.",
                      deliverable_paths=[FINAL, NOTES], remaining_roles=[]),
                 write(FINAL, ARTICLE), write(NOTES, NOTE), {"text": "Article and notes ready."},
                 {"text": "The notes were checked previously; ready."},
                 tool("delegate_task", assignee="checker", objective="Check notes.",
                      review={**review, "criteria": [{**RULE, "method": "inspect"}]}),
                 tool("delegate_task", assignee="writer", objective="Approve my own article.", review=review),
                 tool("delegate_task", assignee="checker", objective="Verify current article.", review=review),
                 tool("read_file", path=NOTES), tool("http_get", url=URL), submit(files, NOTES),
                 tool("read_file", path=FINAL), submit(files, FINAL), {"text": "Published assessment."},
                 {"text": "The final article is ready."}]
        detail, provider = self.execute(steps)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        messages = str(provider.requests)
        for expected in ("指定成稿当前版本缺少独立来源核查", "请加入必需的 source 标准", "请选择本群其他成员",
                         "引用实际读过的成稿和本次打开的原始来源"):
            self.assertIn(expected, messages)
        report = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"][-1]
        review = report["native_review"]
        self.assertEqual(review["state"], "passed")
        receipts = review["assessment"]["results"][0]["receipts"]
        self.assertEqual([(r["path"], r["sha256"]) for r in receipts if r["kind"] == "read"], [(FINAL, files[FINAL])])
        self.assertEqual([r["url"] for r in receipts if r["kind"] == "source"], [URL])

    def test_source_repair_author_can_substantiate_original_fact_for_independent_resignature(self):
        files = {FINAL: hashed(ARTICLE)}
        revision = ARTICLE + "\nSigned dispatch order, first sentence: 'This shipment goes to Main Street.' " + URL
        rejection = tool("submit_review", candidate_version=digest(files), results=[{
            "criterion_id": "claim", "verdict": "changes_requested", "cause": "product",
            "reason": "I interpret the dispatch destination as Mill Street.",
            "action": "Replace Main Street with Mill Street.", "evidence": ["file:" + FINAL, "source:" + URL]}])
        steps = [
            tool("delegate_task", assignee="writer", objective="Publish the dispatch.", deliverable_paths=[FINAL],
                 remaining_roles=["checker"]), write(FINAL, ARTICLE), {"text": "Dispatch published."},
            tool("delegate_task", assignee="checker", objective="Verify the destination.",
                 review={"candidate": FINAL, "criteria": [RULE]}),
            tool("read_file", path=FINAL), tool("http_get", url=URL), rejection, {"text": "Review signed."},
            tool("delegate_task", assignee="writer", objective="Address the finding."),
            tool("http_get", url=URL), write(FINAL, revision), {"text": "The issuer confirms Main Street; see its first sentence."},
            tool("delegate_task", assignee="checker", objective="Verify the revised dispatch."),
            tool("read_file", path=FINAL), tool("http_get", url=URL), submit(files, FINAL),
            {"text": "Released the reviewed dispatch."}]
        detail, provider = self.execute(steps, provider_type=CurrentCandidateProvider)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual([r["role"] for r in reports], ["writer", "checker", "writer", "checker"])
        initial, rejected, repair, signed = reports
        self.assertNotIn("评审建议本身", initial["objective"])
        for phrase in ("评审建议本身不能作为事实依据", "打开原始发布者", "单位和分母", "正负号",
                       "保留已核实的事实", "可定位的引文", "原评审者", "Mill Street"):
            self.assertIn(phrase, repair["objective"])
            self.assertIn(phrase, str(provider.requests[9][0]))
        self.assertNotIn("submit_review", provider.requests[9][1])
        self.assertIn("亲自核对其引文和原文上下文", str(provider.requests[13][0]))
        self.assertEqual(rejected["native_review"]["state"], "repair")
        self.assertEqual(repair["native_review"]["state"], "review")
        decision = signed["native_review"]
        self.assertEqual((decision["state"], decision["reviewer"], decision["files"][FINAL]),
                         ("passed", "checker", hashed(revision)))
        self.assertNotEqual(rejected["native_review"]["candidate_version"], decision["candidate_version"])
        self.assertEqual(decision["assessment"]["candidate_version"], decision["candidate_version"])

    def test_caption_repair_on_sourced_artifact_preserves_the_original_observation(self):
        files = {FINAL: hashed(ARTICLE)}
        updated = ARTICLE + "\nDestination confirmed by the signed dispatch, first sentence."
        caption_rule = {"id":"caption", "method":"inspect", "description":"Label the destination."}
        def sign(caption):
            call = submit(files, FINAL)
            call["tool_calls"][0]["arguments"]["results"].append(caption)
            return call
        rejected = {"criterion_id":"caption", "verdict":"changes_requested", "cause":"product",
                    "reason":"The destination caption is missing.", "action":"Label it Mill Street.",
                    "evidence":["file:" + FINAL]}
        passed = {"criterion_id":"caption", "verdict":"passed", "reason":"The label matches the original.",
                  "evidence":["file:" + FINAL]}
        steps = [tool("delegate_task", assignee="writer", objective="Prepare the dispatch.", deliverable_paths=[FINAL]),
                 write(FINAL, ARTICLE), {"text":"Ready."},
                 tool("delegate_task", assignee="checker", objective="Verify the dispatch and caption.",
                      review={"candidate":FINAL,"criteria":[RULE,caption_rule]}),
                 tool("read_file",path=FINAL), tool("http_get",url=URL), sign(rejected), {"text":"Caption needs a revision."},
                 tool("delegate_task",assignee="writer",objective="Revise the caption."),
                 tool("http_get",url=URL), write(FINAL,updated), {"text":"Retained the original destination."},
                 tool("delegate_task",assignee="checker",objective="Check the corrected version."),
                 tool("read_file",path=FINAL), tool("http_get",url=URL), sign(passed), {"text":"Delivered."}]
        detail, provider = self.execute(steps, provider_type=CurrentCandidateProvider)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual(reports[1]["review_action"]["criteria"], ["caption"])
        self.assertIn("保留正确的口径", reports[2]["objective"])
        self.assertIn("比较各系列的指标定义", str(provider.requests[4][0]))
        self.assertEqual(reports[3]["native_review"]["files"][FINAL], hashed(updated))

    def closing_review(self):
        review = {"candidate": "writer", "criteria": [RULE]}
        return [tool("delegate_task", assignee="writer", objective="Write final article.", deliverable_paths=[FINAL],
                     remaining_roles=["checker"]), write(FINAL, ARTICLE), {"text": "Article delivered."},
                tool("delegate_task", assignee="checker", objective="Verify final article.", review=review),
                tool("http_get", url=URL),
                # Seeing its contents via shell is not a signed read of the original.
                tool("run_shell", command="cat final.md"),
                *[tool("list_review_evidence", offset=i) for i in range(7)]]

    def test_stalled_reviewer_can_acquire_specific_missing_read_and_submit_short_ids(self):
        version = digest({FINAL: hashed(ARTICLE)})
        def claim(refs):
            return tool("submit_review", candidate_version=version, results=[{"criterion_id":"claim",
                "verdict":"passed", "reason":"Compared the destination in the article to the dispatch.", "evidence":refs}])

        class ShortReferences(RecordingProvider):
            def chat(self, messages, tools=None, temperature=0):
                if self.cursor < len(self.script) and self.script[self.cursor].get("use_observed_refs"):
                    for message in reversed(messages):
                        text = message.get("content") or ""
                        pos = text.find('{"recent_evidence"') if isinstance(text, str) else -1
                        if pos != -1:
                            catalog = json.JSONDecoder().raw_decode(text[pos:])[0]["recent_evidence"]
                            read, = [ref for ref, path in catalog["read"] if path == FINAL]
                            source, = [ref for ref, url in catalog["source"] if url == URL]
                            self.script[self.cursor] = claim([read, source])
                            break
                return super().chat(messages, tools, temperature)

        start = self.closing_review()
        steps = start + [claim(["source:" + URL]), tool("read_lines", path=FINAL), {"use_observed_refs":True},
                         {"text":"The article checks out."}, {"text":"Delivered."}]
        detail, provider = self.execute(steps, provider_type=ShortReferences)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertIn("read_lines", provider.requests[len(start)][1])
        self.assertIn("submit_review", provider.requests[len(start)][1])
        self.assertIn("list_review_evidence", provider.requests[len(start) + 1][1])
        self.assertEqual(provider.requests[len(start) + 2][1], provider.requests[len(start)][1])
        correction = str(provider.requests[len(start) + 1][0])
        self.assertIn("本次亲自打开的来源", correction)
        self.assertIn('"claim":[{"tool":"read_lines","paths":["final.md"]}]', correction)
        report = [e["handoff"]["native_review"] for e in detail["room_events"]
                  if e["kind"] == "member_report" and "native_review" in e["handoff"]][-1]
        finding, = report["assessment"]["results"]
        self.assertTrue(all(ref.startswith("E") for ref in finding["evidence"]))
        read, source = finding["receipts"]
        self.assertEqual([read["ref"], source["ref"]], finding["evidence"])
        self.assertEqual((read["path"], read["sha256"], source["url"]), (FINAL, hashed(ARTICLE), URL))
        self.assertTrue(source["observed_bytes_sha256"])

    def test_reviewer_can_report_blocked_with_tools_and_without_passing_review(self):
        steps = self.closing_review() + [tool("read_lines", path=f"absent-{i}.md") for i in range(3)]
        steps += [{"text":"[BLOCKED] Could not verify the original file."},
                  {"text":"[BLOCKED] Source criterion is still outstanding."}]
        detail, provider = self.execute(steps)
        self.assertNotEqual(detail["run"]["status"], "completed")
        messages, tools = provider.requests[len(steps) - 2]
        self.assertIn("read_lines", tools)
        self.assertIn("submit_review", tools)
        self.assertIn("进度提醒", str(messages))
        self.assertIn("absent-2.md", str(messages))
        self.assertFalse(any(e.get("handoff", {}).get("native_review", {}).get("state") == "passed"
                             for e in detail["room_events"]))

    def test_same_url_with_distinct_content_requires_exact_short_ref_and_errors_identify_each_rule(self):
        file_hash = hashed(ARTICLE)
        manifest = {FINAL:file_hash}
        rules = [Rule.model_validate(RULE), Rule(id="runtime", description="Execute check", method="execute")]
        item = Review("target", "writer", "checker", "Review.", rules, manifest.copy(), required_source_paths={FINAL})
        progress = Mock()
        progress.project_versions.return_value = manifest
        progress.review_evidence.return_value = {}
        progress.evidence.return_value = {"diagnostic_checks":[]}
        session = ReviewSession(item, manifest.copy(), progress, "attempt")
        def observe(tool, call, **meta):
            session.observe(SimpleNamespace(observations=[SimpleNamespace(ok=True, source=tool, tool_call_id=call, meta=meta)]))
        observe("read_lines", "read", path=FINAL, sha256=file_hash, start_line=1, returned_lines=1)
        for body in ("first signed dispatch", "updated dispatch"):
            sha = hashed(body)
            path = f"sources/{sha}.json"
            manifest[path] = sha
            observe("http_get", body, path=path, url=URL, bytes=len(body), observed_bytes_sha256=sha)
        read, first, second = session.catalog()["evidence"]
        self.assertNotEqual(first["ref"], second["ref"])
        self.assertNotEqual(first["sha256"], second["sha256"])
        submitted = {"candidate_version":item.version, "results":[
            {"criterion_id":"claim", "verdict":"passed", "reason":"Compared the dispatch.", "evidence":[read["ref"], "source:" + URL]},
            {"criterion_id":"runtime", "verdict":"passed", "reason":"Executed.", "evidence":[read["ref"]]}]}
        rejected = session.submit(None, submitted)
        self.assertEqual([e["criterion_id"] for e in rejected.meta["issues"]], ["claim", "runtime"])
        self.assertIsNone(session.assessment)
        submitted["results"][0]["evidence"][1] = second["ref"]
        submitted["results"][1].update(verdict="inconclusive", cause="verification", action="Run the actual check.")
        self.assertTrue(session.submit(None, submitted).ok)
        received = session.assessment["results"][0]["receipts"][1]
        self.assertEqual((received["tool_call_id"], received["url"], received["sha256"]),
                         ("updated dispatch", URL, hashed("updated dispatch")))

    def test_working_notes_alone_cannot_complete_final_delivery(self):
        steps = [tool("delegate_task", assignee="writer", objective="Write research notes.", deliverable_paths=[NOTES],
                      remaining_roles=["checker"]), write(NOTES, NOTE), {"text": "Notes ready."},
                 tool("delegate_task", assignee="checker", objective="Check notes.",
                      review={"candidate": "writer", "criteria": [RULE]}),
                 tool("read_file", path=NOTES), tool("http_get", url=URL), submit({NOTES: hashed(NOTE)}, NOTES),
                 {"text": "Verified notes."}, {"text": "[BLOCKED] No final article was delivered."}]
        detail, _ = self.execute(steps)
        reviews = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual(reviews[-1]["native_review"]["state"], "passed")
        self.assertEqual(detail["run"]["status"], "failed")

    def test_later_editor_must_get_a_new_review_on_changed_delivery(self):
        new = ARTICLE + " The dispatch order was confirmed."
        review = {"candidate": "writer", "criteria": [RULE]}
        steps = [tool("delegate_task", assignee="writer", objective="Write dispatch article.",
                      deliverable_paths=[FINAL], remaining_roles=["checker", "reviser"]),
                 write(FINAL, ARTICLE), {"text": "First draft ready."},
                 tool("delegate_task", assignee="checker", objective="Verify draft.", review=review),
                 tool("read_file", path=FINAL), tool("http_get", url=URL), submit({FINAL: hashed(ARTICLE)}, FINAL),
                 {"text": "First draft verified."},
                 tool("delegate_task", assignee="reviser", objective="Add confirmation.", deliverable_paths=[FINAL]),
                 write(FINAL, new), {"text": "New version ready."},
                 tool("delegate_task", assignee="checker", objective="Verify changed text."),
                 tool("read_file", path=FINAL), tool("http_get", url=URL),
                 submit({FINAL: hashed(ARTICLE)}, FINAL), submit({FINAL: hashed(new)}, FINAL),
                 {"text": "The new version agrees with the dispatch order."}, {"text": "Delivery complete."}]
        detail, provider = self.execute(steps, members=("lead", "writer", "checker", "reviser"))
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        signed = [e["handoff"]["native_review"]["assessment"] for e in detail["room_events"]
                  if e["kind"] == "member_report" and e["role"] == "checker"]
        self.assertEqual([a["candidate_version"] for a in signed],
                         [digest({FINAL: hashed(ARTICLE)}), digest({FINAL: hashed(new)})])
        self.assertIn("候选版本已改变", str(provider.requests))

    def test_original_browser_replay_on_revision_requires_new_independent_source_review(self):
        path = "dispatch.html"
        initial = "<!doctype html><h1>Dispatch to Main St.</h1>"
        revised = initial.replace("Main St.", "Main Street")
        initial_version, new_version = {path: hashed(initial)}, {path: hashed(revised)}
        open_page = "from pathlib import Path\npage.goto(Path('dispatch.html').resolve().as_uri())\n"
        original_probe = open_page + "assert 'Main Street' in page.locator('h1').inner_text()"
        different_probe = open_page + "assert page.locator('h1').count() == 1"
        first_probe = tool("run_browser", code=original_probe)
        steps = [tool("delegate_task", assignee="writer", objective="Publish the dispatch destination.",
                      deliverable_paths=[path], remaining_roles=["checker", "release"]),
                 write(path, initial), {"text": "Dispatch published."},
                 tool("delegate_task", assignee="checker", objective="Check the destination.",
                      review={"candidate": "writer", "criteria": [RULE]}),
                 tool("read_file", path=path), tool("http_get", url=URL), submit(initial_version, path),
                 {"text": "Verified the initial destination."},
                 tool("delegate_task", assignee="release", objective="Use the full street name and verify the rendered page.",
                      deliverable_paths=[path]), first_probe]
        # The scripted provider assigns real tool call IDs from its turn counter.
        original_call_id = f"call_{len(steps)}_0"
        steps += [tool("edit_file", path=path, old_text="Main St.", new_text="Main Street"),
                  tool("run_browser", code=different_probe),
                  tool("rerun_browser_check", tool_call_id=original_call_id),
                  {"text": "Full spelling verified with the original browser test."},
                  tool("delegate_task", assignee="checker", objective="Check the published revision."),
                  tool("read_file", path=path), tool("http_get", url=URL),
                  submit(initial_version, path), submit(new_version, path),
                  {"text": "The revised page matches the source."}, {"text": "Delivered the reviewed revision."}]
        detail, _ = self.execute(steps, members=("lead", "writer", "checker", "release"), final_path=path)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        trace_path = self.root/"runs"/detail["run"]["id"]/"trace.jsonl"
        trace = [json.loads(line) for line in trace_path.read_text().splitlines()]
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertTrue(all(r["published"] for r in reports))
        release = next(r for r in reports if r["role"] == "release")
        evidence = [e for e in trace if e.get("task_id") == release["task_id"]]
        probes = [e for e in evidence if e["kind"] == "tool_result" and e["tool"] in {"run_browser", "rerun_browser_check"}]
        self.assertEqual([(e["tool"], e["exit_code"]) for e in probes],
                         [("run_browser", 1), ("run_browser", 0), ("rerun_browser_check", 0)])
        self.assertEqual(probes[-1]["replayed_tool_call_id"], probes[0]["tool_call_id"])
        checkpoints = {e["step"]: e["checkpoint"] for e in evidence if e["kind"] == "member_work_checkpoint"}
        self.assertTrue(checkpoints[probes[1]["step"]]["failed"])
        self.assertEqual(checkpoints[probes[-1]["step"]]["failed"], [])
        reviews = [r["native_review"]["assessment"] for r in reports if r["role"] == "checker"]
        self.assertEqual([x["candidate_version"] for x in reviews], [digest(initial_version), digest(new_version)])
        second_checker = next(r["task_id"] for r in reversed(reports) if r["role"] == "checker")
        decisions = [e for e in trace if e["kind"] == "tool_result" and e["tool"] == "submit_review"
                     and e["task_id"] == second_checker]
        self.assertEqual([e["ok"] for e in decisions], [False, True])
        self.assertIn("候选版本已改变", decisions[0]["preview"])
        original_receipts, revised_receipts = [x["results"][0]["receipts"] for x in reviews]
        self.assertEqual([r["sha256"] for r in revised_receipts if r["kind"] == "read"], [hashed(revised)])
        self.assertNotEqual(next(r["receipt_id"] for r in original_receipts if r["kind"] == "source"),
                            next(r["receipt_id"] for r in revised_receipts if r["kind"] == "source"))

    def test_new_version_invalidates_receipt_and_no_op_handoff_cannot_hide_authorship(self):
        old, new = hashed(ARTICLE), hashed(ARTICLE + " Updated.")
        queue = ReviewQueue([FINAL])
        a = {"task_id": "first", "role": "writer", "objective": "Write.", "deliverable_paths": [FINAL]}
        queue.published(a, {FINAL: old}, [{"path": FINAL, "sha256": old}])
        r = queue.open({"candidate": "first", "criteria": [RULE]}, "checker", [a])
        queue.items[r.target] = r
        r.apply({"candidate_version": r.version, "results": [{"criterion_id":"claim", "verdict": "passed",
                 "receipts":[{"kind":"read", "path": FINAL, "sha256": old, "extent": 8}, {"kind":"source"}]}]}, "[]")
        self.assertEqual(queue.missing_source_reviews(), [])
        edit = {**a, "task_id":"updated"}
        queue.published(edit, {FINAL: new}, [{"path": FINAL, "sha256": new}])
        self.assertEqual(queue.missing_source_reviews(), [FINAL])
        proxy = {**edit, "task_id":"proxy", "role":"checker"}
        queue.published(proxy, {FINAL: new}, [])
        with self.assertRaisesRegex(ValueError, "这名成员写入"):
            queue.open({"candidate":"proxy","criteria":[RULE]}, "writer", [edit, proxy])

    def test_invalid_paths_and_unavailable_peer_rejected_before_start(self):
        for value in ("x.md", [None], [FINAL, FINAL], ["../unsafe.md"], ["node_modules/page.md"], ["picture.png"]):
            with self.subTest(value=value), self.assertRaises(StudioError):
                self.service.start(request("team", objective="Publish", conversation=roster("lead", "writer", "checker"),
                                           required_source_review_paths=value))
        with self.assertRaises(StudioError):
            self.service.start(request("single", objective="Publish", conversation={"kind":"dm","members":["writer"],
                                       "recipient":"writer","intent":"edit"},
                                       required_source_review_paths=[FINAL]))
        legacy = request("team", objective="Publish", conversation=roster("lead", "writer"))
        self.assertEqual(self.service._request(legacy), self.service._request({**legacy,"required_source_review_paths":[]}))

    def test_two_people_can_write_and_independently_verify_sources_with_lead_as_reviewer(self):
        result, _ = self.execute([
            tool("delegate_task", assignee="writer", objective="Write the shipment article.",
                 deliverable_paths=[FINAL], remaining_roles=["lead"]),
            write(FINAL, ARTICLE), {"text":"The article is ready."},
            tool("delegate_task", assignee="lead", objective="Independently inspect the article and source.",
                 review={"candidate":FINAL, "criteria":[RULE]}),
            tool("read_file", path=FINAL), tool("http_get", url=URL), submit({FINAL:hashed(ARTICLE)}, FINAL),
            {"text":"Reviewed."}, {"text":"Ready."}], members=("lead","writer"))
        self.assertEqual(result["run"]["status"], "completed", result["diagnostics"])
        reports = [r["handoff"] for r in result["room_events"] if r["kind"] == "member_report"]
        self.assertEqual(reports[-1]["native_review"]["reviewer"], "lead")
        self.assertEqual(reports[-1]["native_review"]["author"], "writer")

    def test_candidate_alias_and_task_id_do_not_make_an_incidental_snapshot_the_comparison_target(self):
        queue = ReviewQueue([FINAL])
        archive = "sources/archive.json"
        files = {FINAL: hashed(ARTICLE), archive: hashed("{}")}
        author = {"task_id": "published", "role": "writer", "objective": "Compile article and archive.",
                  "deliverable_paths": list(files)}
        queue.published(author, files, [{"path": p, "sha256": sha} for p, sha in files.items()])
        comparison = {"id":"structure", "description":"Inspect the final article's structure.", "method":"inspect"}
        for selector in (FINAL, "writer", "published"):
            with self.subTest(selector=selector):
                with self.assertRaisesRegex(ValueError, "请省略 reference_path"):
                    queue.open({"candidate": selector, "criteria": [RULE, {**comparison, "reference_path": FINAL}]},
                               "checker", [author])
                revised = queue.open({"candidate": selector, "criteria": [RULE, comparison]}, "checker", [author])
                self.assertEqual(revised.files, files)
                self.assertEqual(revised.comparison_targets, {FINAL})

    def test_distinct_reference_and_actual_manuscript_must_both_be_in_the_comparison_receipts(self):
        archive = "sources/dispatch.json"
        files = {FINAL: hashed(ARTICLE), NOTES: hashed(NOTE), archive: hashed("{}")}
        rule = {"id": "destination", "description": "Compare the manuscript with the original notes.", "method": "inspect"}
        def review(reference):
            return tool("delegate_task", assignee="checker", objective="Inspect the manuscript.",
                        review={"candidate": FINAL, "criteria": [RULE, {**rule, "reference_path": reference}]})
        def decision(read):
            call = submit(files, FINAL)
            call["tool_calls"][0]["arguments"]["results"].append({"criterion_id":"destination", "verdict":"passed",
                 "reason":"Checked the manuscript against the dispatch notes.", "evidence":["file:"+read, "reference:"+NOTES]})
            return call
        detail, provider = self.execute([
            tool("delegate_task", assignee="writer", objective="Write the manuscript and preserve the source and notes.",
                 deliverable_paths=list(files), remaining_roles=["checker"]),
            write(FINAL, ARTICLE), write(NOTES, NOTE), write(archive, "{}"), {"text":"Documents ready."},
            review(FINAL), review(NOTES), tool("read_file", path=archive), tool("read_file", path=NOTES),
            tool("read_file", path=FINAL), tool("http_get", url=URL), decision(archive), decision(FINAL), {"text":"Delivered."}])
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertIn("请省略 reference_path", str(provider.requests))
        self.assertIn("本地对照须读取并同时引用候选成稿", str(provider.requests))
        event, = [e for e in detail["room_events"] if e["kind"] == "member_report" and e["role"] == "checker"]
        receipts = event["handoff"]["native_review"]["assessment"]["results"][1]["receipts"]
        self.assertEqual([(r["kind"], r["path"]) for r in receipts], [("read", FINAL), ("reference", NOTES)])

    def test_addressed_worker_gets_work_tools_and_must_obtain_fresh_independent_source_approval(self):
        review = {"candidate": FINAL, "criteria": [RULE]}
        detail, provider = self.execute([
            tool("http_get", url=URL), write(FINAL, ARTICLE), {"text": "成稿和原始来源都查过了。"},
            {"text": "仅凭我自己的来源检查，尝试再次收尾。"},
            tool("delegate_task", assignee="researcher", objective="Approve the manuscript.", review=review),
            tool("delegate_task", assignee="checker", objective="Check the final dispatch.",
                 review={"candidate": FINAL, "criteria": [{**RULE, "method": "inspect"}]}),
            tool("delegate_task", assignee="checker", objective="Verify against the original source.", review=review),
            tool("read_file", path=FINAL), submit({FINAL: hashed(ARTICLE)}, FINAL),
            tool("http_get", url=URL), submit({FINAL: hashed(ARTICLE)}, FINAL), {"text": "已交付。"}],
            members=("lead", "researcher", "checker"), objective="@researcher 请自己修改成稿，并安排独立核查来源。",
            provider_type=CurrentCandidateProvider)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertEqual(detail["run"]["mode"], "single")
        self.assertTrue({"search_sources", "http_get", "write_file", "edit_file", "delegate_task"} <= set(provider.requests[0][1]))
        self.assertIn("本轮必须独立核查来源的成稿", str(provider.requests[0][0]))
        feedback = str(provider.requests)
        for message in ("指定成稿当前版本缺少独立来源核查", "原作者不能独立验收", "必需的 source 标准", "未观察到"):
            self.assertIn(message, feedback)
        direct, = [e for e in detail["room_events"] if e["kind"] == "direct_delivery"]
        self.assertEqual((direct["role"], direct["task_id"]), ("researcher", detail["run"]["id"]))
        report, = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        approved = report["native_review"]
        self.assertEqual((approved["author"], approved["reviewer"], approved["files"][FINAL], approved["state"]),
                         ("researcher", "checker", hashed(ARTICLE), "passed"))
        self.assertEqual(approved["assessment"]["candidate_version"], digest(approved["files"]))

    def test_named_root_can_delegate_writing_and_cannot_skip_an_explicit_child_check(self):
        review = {"candidate": FINAL, "criteria": [RULE]}
        detail, provider = self.execute([
            tool("http_get", url=URL), write(NOTES, NOTE), {"text": "研究笔记已好，尝试收尾。"},
            tool("delegate_task", assignee="writer", objective="Write the manuscript.", remaining_roles=[]),
            write(FINAL, ARTICLE), {"text": "已交稿。"}, {"text": "稿件已存在，尝试收尾。"},
            tool("delegate_task", assignee="checker", objective="Independently verify the writer's claims.", review=review),
            tool("read_file", path=FINAL), tool("http_get", url=URL), submit({FINAL: hashed(ARTICLE)}, FINAL),
            {"text": "核查完毕。"}], members=("researcher", "writer", "checker"),
            objective="@researcher 请按需要邀请作者和来源核查者。", child_checks={"writer": {"files": [FINAL]}})
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertEqual(detail["run"]["mode"], "single")
        self.assertIn("这些成员的交接尚未完成或未通过用户验收：writer", str(provider.requests))
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual([h["role"] for h in reports], ["writer", "checker"])
        self.assertEqual((reports[0]["status"], reports[1]["native_review"]["author"]), ("completed", "writer"))
