"""Real Studio UI and HTTP service; deterministic provider only, temporary workspace."""
from pathlib import Path
import sqlite3
from PIL import Image
from tempfile import TemporaryDirectory
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.studio_server import create_server
from miniagent.studio_service import StudioService


def call(name, **arguments):
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


def main():
    with TemporaryDirectory() as scratch:
        project = (Path(scratch) / "fixture project").resolve()
        project.mkdir()
        (project / "a.html").write_text("<h1>Room fixture preview</h1>", encoding="utf-8")
        Image.new("RGB", (41, 29), "teal").save(project / "room-screen.png")
        db = sqlite3.connect(project / "reservations.db")
        db.execute("create table reservations (room text)")
        db.commit()
        db.close()
        service = StudioService(Path(scratch) / "runs", Settings(api_key="offline-fixture-key"))
        server = create_server(service, port=0)
        first = ScriptedProvider([
            call("delegate_task", assignee="reviewer", chat_message="@reviewer 问清时间范围，再把结果记下来。",
                 objective="确认报告时间范围。\n" + ("输出说明与期限依据。" * 235) + "材料末尾。", remaining_roles=[]),
            call("ask_human", question="报告的时间范围是多少？"),
            call("write_file", path="working.md", content="Unpublished working version of the proposal."),
            {"text": "交接完成，下面是给负责人的交接说明。\n\n## 一句话成果\n已记录两年期限。\n\n## 工作记录\n已保存 working.md。"},
            {"error": "intentional fixture interruption"},
        ])
        followup = ScriptedProvider([
            call("read_file", path="working.md"),
            call("append_file", path="working.md", content="\nThe follow-up revision is complete."),
            {"text": "已接续并完成修订。"},
        ])
        with (patch("miniagent.studio_service.DeepSeekProvider", side_effect=[first, followup]),
              patch("miniagent.studio_workspace.pick_directory", return_value=str(project))):
            print(f"http://127.0.0.1:{server.server_port}/", flush=True)
            try:
                server.serve_forever()
            finally:
                server.server_close()
                service.close()


if __name__ == "__main__":
    main()
