"""Replay a completed room's recorded delivery through the local Studio chat API.

python -m tests.replay_room_followup /path/to/runs/groups/g-...
The provider reuses the archived final answer. No model or network service is required.
"""
import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from miniagent.config import Settings
from miniagent.server import serve
from miniagent.studio import Studio
from tests.helpers import FakeProvider, reply
from tests.test_server import request


def hashes(folder):
    return {f.relative_to(folder).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(folder.rglob("*")) if f.is_file()}


def replay(group):
    group = group.resolve()
    completed = [json.loads(p.read_bytes()) for p in group.glob("tasks/*/task.json")]
    previous = max((t for t in completed if t["status"] == "completed" and t.get("final_answer")),
                   key=lambda t:t.get("finished_at") or t["created_at"])
    question = "你都做了啥"
    def playback(agent, messages, tools, options):
        # Reuse the recorded model's words only if the service supplied the matching room evidence.
        assert agent == "room" and messages[-1]["content"] == question
        data = json.loads(messages[1]["content"].split("\n", 1)[1])
        delivery = next(t for t in data["tasks"] if t["id"] == previous["id"])
        assert delivery["file_changes"] and previous["final_answer"].startswith(delivery["final_answer"])
        return reply(delivery["final_answer"])

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        copied = root / "groups" / group.name
        shutil.copytree(group, copied, symlinks=True, ignore=shutil.ignore_patterns("agents"))
        config_path = copied / "group.json"
        config = json.loads(config_path.read_bytes())
        if config.get("folder"):
            config["folder"] = ""
            config_path.write_text(json.dumps(config, ensure_ascii=False))
        for role in config.get("role_ids", []):
            shutil.copytree(group.parents[1] / "roles" / role, root / "roles" / role)
        provider = FakeProvider(playback)
        service = Studio(Settings(runs_dir=root), lambda _:provider)
        main, preview = serve(service,0)
        try:
            base = f"http://127.0.0.1:{main.server_port}/api/groups/{group.name}"
            def observe():
                _,_,raw = request(base)
                state = json.loads(raw)
                manifest = hashes(copied / "project")
                digest = hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
                return state, (state["group"]["tasks"], len(manifest), digest), hashes(copied / "tasks")
            state, before, tasks_before = observe()
            seq = state["events"][-1]["seq"]
            status,_,raw = request(base+"/messages","POST",{"text":question})
            receipt = json.loads(raw)
            state, after, tasks_after = observe()
            events = [e for e in state["events"] if e["seq"] > seq]
            assert status == 200 and receipt["route"] == "reply", receipt
            assert [e["kind"] for e in events] == ["user_message","room_reply"]
            assert events[-1]["in_reply_to"] == events[0]["seq"]
            assert receipt["text"] == events[-1]["text"] and previous["final_answer"].startswith(receipt["text"])
            assert before == after and tasks_before == tasks_after and state["live"] is None
            assert len(provider.requests) == 1
            return {"group":group.name,"delivery_task":previous["id"],"question":question,
                    "route":receipt["route"],"model":"offline replay of archived final answer",
                    "tasks_before_after":[before[0],after[0]],"files_before_after":[before[1],after[1]],
                    "manifest_sha256_before_after":[before[2],after[2]],
                    "new_events":[e["kind"] for e in events],"answer":receipt["text"]}
        finally:
            service.close()
            for server in (main,preview):
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", type=Path)
    print(json.dumps(replay(parser.parse_args().group),ensure_ascii=False,indent=2))
