"""Offline, disposable Studio server for role-library browser tests."""
from pathlib import Path
import json
from tempfile import TemporaryDirectory
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.studio_server import create_server
from miniagent.studio_service import StudioService
from tests.test_studio_service import finished, request


class CheckIndependentChat(ScriptedProvider):
    def chat(self, messages, tools=None, temperature=0):
        context = json.dumps(messages, ensure_ascii=False)
        assert "保留两份 <本地备份>。" in context, "The new chat has lost the role's edited memory."
        assert "核对发布清单。" not in context, "Knowledge scoped to the first project leaked into a new project."
        assert "重启应用，确认已有预约仍然存在。" not in context
        assert "优先运行快速测试。" not in context, "Rejected knowledge entered the model input."
        return super().chat(messages, tools, temperature)


def main():
    with TemporaryDirectory() as scratch:
        service = StudioService(Path(scratch) / "runs", Settings(api_key="offline-fixture"))
        server = create_server(service, port=0)
        project = Path(scratch, "product").resolve()
        project.mkdir()
        def propose(kind, title, text):
            return {"tool_calls": [{"name": "propose_role_knowledge", "arguments": {
                "kind": kind, "title": title, "text": text, "scope": "project",
            }}]}

        # These valid inline room roles existed before the user saved a long-lived profile.
        legacy = {"id":"legacy","name":"旧版维护助手","prompt":"核对本机数据。"}
        for title, prompt in (("旧项目交接记录", legacy["prompt"]), ("另一位同名成员", "负责写作。")):
            with patch("miniagent.studio_service.DeepSeekProvider",
                       return_value=ScriptedProvider([{"text":"已记录原对话。"}])):
                old = service.start(request("single", objective=title, conversation={
                    "kind":"dm","members":["legacy"],"recipient":"legacy","intent":"question",
                    "profiles":[{**legacy,"prompt":prompt}],
                }))
                finished(service, old["id"])
                old_meta = service.runs_dir / old["id"] / "studio.json"
                data = json.loads(old_meta.read_text())
                data["thread"].pop("saved_roles", None)
                old_meta.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        service.add_profile(legacy)
        first = ScriptedProvider([
            propose("skill", "重启验证", "重启应用，确认已有预约仍然存在。"),
            propose("memory", "测试偏好", "优先运行快速测试。"),
            {"text": "已提交两项建议。"},
        ])
        second = CheckIndependentChat([{"text": "已收到本轮消息。"}])
        continuation = ScriptedProvider([{"text":"旧项目已继续。"}])
        with (patch("miniagent.studio_service.DeepSeekProvider", side_effect=[first, second, continuation]),
              patch("miniagent.studio_workspace.pick_directory", return_value=str(project))):
            print(f"http://127.0.0.1:{server.server_port}/", flush=True)
            try:
                server.serve_forever()
            finally:
                server.server_close()
                service.close()


if __name__ == "__main__":
    main()
