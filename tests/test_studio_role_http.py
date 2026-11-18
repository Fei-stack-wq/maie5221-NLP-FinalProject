"""Loopback HTTP contract from profile creation through review of a real tool proposal."""
import unittest
import time
import uuid
from unittest.mock import patch

from miniagent.providers import ScriptedProvider
from tests import test_studio_chat_http as chat_http
from tests.test_studio_conversations import RecordingProvider


class StudioRoleHTTPTest(unittest.TestCase):
    # Reuse the actual localhost server, CSRF bootstrap and asynchronous run polling.
    setUp = chat_http.StudioChatHTTPTest.setUp
    exchange = chat_http.StudioChatHTTPTest.exchange
    get = chat_http.StudioChatHTTPTest.get
    send = chat_http.StudioChatHTTPTest.send

    def post(self, path, data, expected=200):
        status, result = self.exchange("POST", path, data)
        self.assertEqual(status, expected, result)
        return result

    def profile(self, role):
        return self.post("/api/profiles", {"id":role,"name":role,"prompt":"Review project records."})["profile"]

    def chat(self, message, conversation):
        provider = RecordingProvider([{"text":"Received the request."}])
        with patch("miniagent.studio_service.DeepSeekProvider",return_value=provider):
            rid, detail = self.send(message,conversation)
        self.assertEqual(detail["run"]["status"],"unverified",detail["diagnostics"])
        return rid, str(provider.requests[0][0])

    def test_profile_edit_refuses_stale_snapshot_and_knowledge_writes_require_browser_token(self):
        first = self.profile("accounting")
        self.post("/api/profiles",{**first,"name":"Other identity"},409)
        endpoint = "/api/profiles/accounting/edit"
        initial = {"name":first["name"],"prompt":first["prompt"]}
        update = {"name":"数据分析师","prompt":"Preserve original records.","previous":initial}
        edited = self.post(endpoint, update)["profile"]
        self.assertEqual(edited["id"],first["id"])
        self.assertEqual(edited["name"],"数据分析师")
        self.post(endpoint,{**update,"name":"Stale edit"},409)
        self.assertIn(edited,self.get("/api/profiles")["profiles"])

        path = "/api/profiles/accounting/knowledge"
        token, self.token = self.token, "untrusted"
        try:
            self.post(path,{"kind":"memory","text":"untrusted write"},403)
        finally:
            self.token = token
        self.assertEqual(self.get(path)["entries"],[])

    def test_real_model_proposal_has_host_source_and_stays_private_until_review(self):
        self.profile("auditor")
        self.profile("engineer")
        path = "/api/profiles/auditor/knowledge"
        other = "/api/profiles/engineer/knowledge"
        form = {"kind":"skill","scope":"role","title":"Compare totals","text":"Read the original records."}
        first = self.post(path,form)["entry"]
        entry = first["id"]
        self.assertEqual((first["revision"],first["source"],first["source_run"]),(1,"user",""))
        for control in ({"role":"engineer"},{"source":"agent"},{"source_run":"forged-id"},
                        {"status":"candidate"},{"active_revision":17}):
            self.post(path,{**form,**control},400)
        self.post(path,{**form,"text":"another update","entry_id":entry,"expected_revision":5},409)
        self.post(other,{**form,"entry_id":entry,"expected_revision":1},404)

        def proposal(text, parent=None):
            conv = ({"parent_run_id":parent,"recipient":"auditor","intent":"edit"} if parent else
                    {"kind":"dm","members":["auditor"],"recipient":"auditor","intent":"edit"})
            invocation = chat_http.tool("propose_role_knowledge",kind="skill",title="Compare totals",scope="role",
                                        text=text,entry_id=entry,base_revision=1)
            provider = ScriptedProvider([invocation,{"text":"Submitted for review."}])
            with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
                run_id, detail = self.send("Propose a better totals check",conv)
            self.assertEqual(detail["run"]["status"],"unverified",detail["diagnostics"])
            return run_id

        first_run = proposal("Propose an unapproved shortcut.")
        lib = self.get(path)
        candidate, = lib["entries"]
        self.assertEqual((candidate["status"],candidate["revision"],candidate["active_revision"]),("candidate",2,1))
        self.assertEqual((candidate["source"],candidate["source_run"]),("agent",first_run))
        self.assertEqual(lib["active_entries"][0]["text"],form["text"])
        self.assertTrue(any(room["id"] == first_run for room in lib["conversations"]))
        self.assertEqual(self.get(other)["entries"],[])
        self.assertEqual(self.exchange("GET",f"{other}/{entry}/history")[0],404)
        self.post(f"{other}/{entry}/status",{"expected_revision":2,"status":"active"},404)
        status = f"{path}/{entry}/status"
        self.post(status,{"status":"active","expected_revision":1},409)
        self.post(status,{"status":"rejected","expected_revision":2})
        self.assertEqual(self.get(path)["active_entries"][0]["revision"],1)

        second_run = proposal("Compute totals from source, then compare after cancellation.",first_run)
        current = self.post(status,{"status":"active","expected_revision":4})["entry"]
        self.assertEqual((current["active_revision"],current["source_run"]),(5,second_run))
        self.assertIn("after cancellation",self.get(path)["active_entries"][0]["text"])
        self.post(f"{path}/{entry}/restore",{"revision":2,"expected_revision":5},409)
        self.post(f"{path}/{entry}/restore",{"revision":1,"expected_revision":4},409)
        self.post(f"{other}/{entry}/restore",{"revision":1,"expected_revision":5},404)
        restored = self.post(f"{path}/{entry}/restore",{"revision":1,"expected_revision":5})["entry"]
        self.assertEqual((restored["revision"],restored["text"],restored["copied_from"]),(6,form["text"],1))
        history = self.get(f"{path}/{entry}/history")["history"]
        self.assertEqual([v["status"] for v in history],
                         ["active","active","candidate","rejected","candidate","active"])
        self.post(status,{"status":"archived","expected_revision":6})
        self.assertEqual(self.get(path)["active_entries"],[])

    def test_compact_lead_receives_own_private_tool_receipt_and_member_stays_isolated(self):
        self.profile("captain")
        self.profile("builder")
        marker = "PRIVATE-LEAD-SKILL-TAIL-42"
        lead = self.post("/api/profiles/captain/knowledge",{
            "kind":"skill","title":"Intake notes","text":("Review evidence for this task. "*85)+marker})["entry"]
        self.post("/api/profiles/builder/knowledge",{"kind":"memory","text":"PRIVATE-BUILDER-NOTE-18"})
        provider = RecordingProvider([
            chat_http.tool("read_role_knowledge",entry_id=lead["id"],revision=1),
            chat_http.tool("delegate_task",assignee="builder",objective="Review the delivery",remaining_roles=[]),
            {"text":"Review delivered."},{"text":"Review handed off."}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            launched = self.post("/api/runs",{
                "request_id":str(uuid.uuid4()),"mode":"team","objective":"Organize a review",
                "conversation":{"kind":"group","members":["captain","builder"],"recipient":"captain","intent":"edit"},
            },202)["run"]
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                result = self.get("/api/runs/" + launched["id"])
                if result["run"]["status"] in {"completed","unverified","failed","cancelled"}:
                    break
                time.sleep(0.02)
        self.assertEqual(result["run"]["status"], "unverified",result["diagnostics"])
        first, lead_after_read, member, _ = provider.requests
        self.assertIn("read_role_knowledge",first[1])
        self.assertNotIn(marker,str(first[0]))
        self.assertIn(marker,str(lead_after_read[0]))
        self.assertNotIn("PRIVATE-BUILDER-NOTE-18",str(lead_after_read[0]))
        self.assertIn("PRIVATE-BUILDER-NOTE-18",str(member[0]))
        self.assertNotIn(marker,str(member[0]))

    def test_explicitly_bind_matching_legacy_conversation_and_reject_stale_or_unrelated_alias(self):
        role = "records"
        old = {"id":role,"name":role,"prompt":"Review project records."}
        def legacy(profile):
            return {"kind":"dm","intent":"edit","members":[role],"recipient":role,"profiles":[profile]}
        # Both rooms predate the saved personal profile. A matching spelling alone
        # never gives the unrelated second role access to the first one's notes.
        # The legacy metadata format predates the explicit saved_roles key.
        # Create actual runs and only substitute that historical write format.
        with patch("miniagent.studio_roles.StudioRoles.bind_roles",side_effect=lambda message: dict(message)):
            first, _ = self.chat("Review the source ledger",legacy(old))
            alias, _ = self.chat("Translate a poem",legacy({**old,"prompt":"Translate German poetry."}))
        self.profile(role)
        path = f"/api/profiles/{role}"
        self.post(path+"/knowledge",{"kind":"memory","text":"PERSONAL-MEMORY-LINK-77"})
        lib = self.get(path+"/knowledge")
        self.assertIn(first, [r["id"] for r in lib["unbound_conversations"]])
        self.assertNotIn(first, [r["id"] for r in lib["conversations"]])
        self.post(path+"/bind",{"room_id":first,"expected_head":alias},409)
        code, body = self.exchange("POST",path+"/bind",{"room_id":alias,"expected_head":alias})
        self.assertIn(code,(400,409),body)
        self.post(path+"/bind",{"room_id":first,"expected_head":first})
        lib = self.get(path+"/knowledge")
        self.assertIn(first,[r["id"] for r in lib["conversations"]])
        self.assertNotIn(first,[r["id"] for r in lib["unbound_conversations"]])
        for rid, allowed in [(first,True),(alias,False)]:
            _, prompt = self.chat("Continue the assignment",{"parent_run_id":rid,"intent":"question","recipient":role})
            self.assertEqual("PERSONAL-MEMORY-LINK-77" in prompt,allowed)

    def test_starting_independent_project_in_same_room_splits_project_knowledge(self):
        self.profile("analyst")
        path = "/api/profiles/analyst/knowledge"
        first,_ = self.chat("Start project A",{"kind":"dm","members":["analyst"],"recipient":"analyst","intent":"edit"})
        [room] = self.get(path)["conversations"]
        self.post(path,{"kind":"memory","scope":"project","project":room["project"],"text":"PRIVATE-PROJECT-A-78"})
        old, prompt = self.chat("Continue project A",{"parent_run_id":first,"recipient":"analyst","intent":"edit"})
        self.assertIn("PRIVATE-PROJECT-A-78",prompt)
        new, prompt = self.chat("Start independent project B",{"parent_run_id":old,"recipient":"analyst",
                                                               "intent":"edit","inherit_files":False})
        self.assertNotIn("PRIVATE-PROJECT-A-78",prompt)
        [new_room] = self.get(path)["conversations"]
        self.assertNotEqual(room["project"], new_room["project"])
        self.post(path,{"kind":"memory","scope":"project","project":new_room["project"],"text":"PRIVATE-PROJECT-B-52"})
        _, prompt = self.chat("Continue project B",{"parent_run_id":new,"recipient":"analyst","intent":"edit"})
        self.assertIn("PRIVATE-PROJECT-B-52",prompt)
        self.assertNotIn("PRIVATE-PROJECT-A-78",prompt)
