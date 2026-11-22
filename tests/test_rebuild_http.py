import base64
import io
import json
import tempfile
import unittest
import urllib.request
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch
from miniagent.server import serve
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_server import request


class RebuildHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.studio = Studio(settings(Path(self.temp.name)),lambda s:FakeProvider(Script(room=[reply("",call("start_room_task",quote="Read the brief"))],lead=[reply("Hello")])))
        self.server,self.preview=serve(self.studio,0)
        self.base="http://127.0.0.1:"+str(self.server.server_port)

    def tearDown(self):
        self.studio.close()
        for server in (self.server,self.preview):
            server.shutdown()
            server.server_close()
        self.temp.cleanup()

    def test_instances_and_preview_cannot_mutate_each_other(self):
        main, preview=serve(self.studio,self.server.server_port)
        try:
            self.assertNotEqual(main.server_port,self.server.server_port)
            first=json.loads(request(self.base+"/api/bootstrap")[2])["token"]
            second=json.loads(request("http://127.0.0.1:"+str(main.server_port)+"/api/bootstrap")[2])["token"]
            self.assertNotEqual(first,second)
            for origin, token in [(self.base,second),("http://127.0.0.1:"+str(self.preview.server_port),first)]:
                req=urllib.request.Request(self.base+"/api/groups",method="POST",data=b"{}",headers={
                    "Origin":origin,"X-Studio-Token":token})
                with self.assertRaises(HTTPError) as error:
                    urllib.request.urlopen(req)
                self.assertEqual(error.exception.code,403)
                error.exception.close()
        finally:
            for server in (main,preview):
                server.shutdown()
                server.server_close()

    def test_running_instance_serves_its_startup_assets(self):
        with tempfile.TemporaryDirectory() as web:
            script = Path(web, "app.js")
            script.write_text("original")
            with patch("miniagent.server.WEB",Path(web)):
                main, preview = serve(self.studio,0)
            try:
                script.write_text("next build")
                Path(web, "later.js").write_text("new asset")
                base = f"http://127.0.0.1:{main.server_port}"
                self.assertEqual(request(base+"/static/app.js")[2],b"original")
                self.assertEqual(request(base+"/static/later.js")[0],404)
                self.assertEqual(json.loads(request(base+"/api/bootstrap")[2])["chat_protocol"],1)
            finally:
                for server in (main,preview):
                    server.shutdown()
                    server.server_close()

    def test_upload_and_assessor_download(self):
        status,_,body=request(self.base+"/api/uploads","POST",
            {"name":"brief.pdf","data":base64.b64encode(b"%PDF-1.0\x00").decode()})
        self.assertEqual(status,201)
        attachment=json.loads(body)
        gid=json.loads(request(self.base+"/api/groups","POST",{"name":"Desk","lead":"lead","members":[
            {"id":"lead","name":"Lead","prompt":"Write."}]})[2])["id"]
        self.assertEqual(request(self.base+"/api/groups/"+gid+"/messages","POST",
            {"text":"Read the brief","attachments":[attachment]})[0],200)
        self.studio.wait(gid)
        with zipfile.ZipFile(io.BytesIO(request(self.base+"/api/groups/"+gid+"/export.zip")[2])) as archive:
            self.assertIn("Read the brief",archive.read("chat.md").decode())
            self.assertIn(attachment["id"],archive.read("chat.jsonl").decode())
        self.assertEqual(request(self.base+"/api/groups/"+gid+"/messages","POST",{"text":"Hi",
            "options":{"member_max_steps":0}})[0],400)
