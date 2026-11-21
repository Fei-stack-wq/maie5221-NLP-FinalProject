"""Two Studio turns, including a frozen member receipt and a withheld private draft."""
import hashlib
import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.studio_server import create_server
from miniagent.studio_service import StudioService
from miniagent.studio_uploads import save_upload
from tests.test_studio_conversations import reply
from tests.test_studio_service import finished, request
from tests.web.serve_room_fixture import call


def main():
    with TemporaryDirectory() as scratch:
        service = StudioService(Path(scratch), Settings(api_key="offline-fixture"))
        first = ScriptedProvider([
            call("write_file", path="docs/Report.md", content="# Published edition"),
            {"text": "已发布 docs/Report.md。另有 ledger.md 和 agents/reviewer/ledger.md，搜索来源 https://example.org/?file=Report.md。长文 Long.md、Giant.md。"},
        ])
        second = ScriptedProvider([{"text": "已核对上一版。"}])
        inputs = {
            "用户笔记.md": "# Uploaded note\n\n**原始记录**\n\n![image](https://example.org/input.png)\n\n<script>window.bad = true</script>".encode(),
            "large-input.md": b"x" * (256 * 1024 + 1),
        }
        attachments = [save_upload(Path(scratch), name, BytesIO(raw), len(raw))["id"] for name, raw in inputs.items()]
        with patch("miniagent.studio_service.DeepSeekProvider", side_effect=[first, second]):
            run = service.start(request("single", objective="出具报告", attachments=attachments, conversation={
                "kind":"group", "members":["worker","reviewer"], "recipient":"worker", "intent":"edit",
            }))
            finished(service, run["id"])
            source = """# Handoff edition

> Editor's notes

| Item | Status |
| --- | --- |
| Review | **Ready** |

1. First action
2. Second action

```text
<sample>
```

[Appendix](../Appendix.md#notes) · [Source](https://example.org/docs)
[Outside](../../Outside.md) · [Local page](/api/bootstrap)

![No request](https://example.org/private.png)

<script>window.untrusted = true</script>
"""
            receipt = Path(scratch) / run["id"] / "handoffs" / ("a" * 32)
            manifest = {}
            for name, content in {"docs/Report.md":source,"Appendix.md":"# Original appendix\n\n## Notes"}.items():
                path = receipt / "project" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            (receipt / "manifest.json").write_text(json.dumps(manifest))
            # The real publication API lists changed files; the full receipt also has
            # unchanged dependencies. A historical link must look these up in its manifest.
            file = {"path":f"handoffs/{receipt.name}/docs/Report.md", "project_path":"docs/Report.md",
                    "sha256":manifest["docs/Report.md"], "size":len(source.encode()), "name":"Report.md"}
            service._record_room_event(run["id"], "reviewer", "member_report",
                                       "交付 [评审报告](docs/Report.md)、`docs/Report.md` 和 docs/Report.md。",
                                       handoff={"published":True}, artifacts=[file])
            service._record_room_event(run["id"], "reviewer", "member_report",
                                       "暂存 `docs/Report.md` 和 [内部记录](private.md)。",
                                       handoff={"published":False}, artifacts=[])
            next_run = service.start(request("single", objective="请核对 docs/Report.md。",
                                             conversation=reply(run["id"], "worker", "question")))
            finished(service, next_run["id"])
            root = Path(scratch) / run["id"] / "agents"
            for role in ("worker", "reviewer"):
                (root / role).mkdir(exist_ok=True)
                (root / role / "ledger.md").write_text("# " + role + " ledger")
            (root / "worker" / "Long.md").write_text("# Long bulletin\n\n" + "detail. " * 35000 + "\n\nLAST LINE")
            (root / "worker" / "Giant.md").write_text("# Giant bulletin\n\n" + "detail. " * 1050000)
        server = create_server(service, port=0)
        print(f"http://127.0.0.1:{server.server_port}/#run/{next_run['id']}/chat", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            service.close()


if __name__ == "__main__":
    main()
