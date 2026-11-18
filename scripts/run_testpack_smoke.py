"""Save a six-turn, four-role offline integration run with real tool execution.

This is a transport/orchestration fixture, NOT an answer to the six original
assignments. No model API, live news, human participant or quality score is used.
Run on Linux/macOS: python -m scripts.run_testpack_smoke --output runs/smoke-1
"""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.studio_service import StudioService
from miniagent.testpack import load_pack, run_pack


def call(name, **arguments):
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


def fixture_pack():
    pack = load_pack(Path(__file__).resolve().parents[1] / "examples/testpack-draft.json")
    pack["execution_kind"] = "scripted_fixture"
    pack["source_note"] += " 本次另将题目替换为离线协作夹具，仅验收协作链路；不能作为原题质量或真实模型实测。"
    for scene in pack["scenarios"]:
        for number, task in enumerate(scene["tasks"], 1):
            task.update(objective=f"OFFLINE FIXTURE: {scene['id']} turn {number}. Ask the fixture responder, compute and independently verify a file, then publish a receipt. All four roles participate.",
                        outputs=[f"{scene['id']}-{number}.json", f"{scene['id']}-{number}.md"],
                        validator=None, checks=[{"kind": "json_equals", "path": f"{scene['id']}-{number}.json",
                                                "pointer": "/sum", "expected": sum(range(1, 11))}])
    return pack


def script(scene, task, previous):
    workers = [p["id"] for p in scene["profiles"] if p["id"] != scene["lead"]]
    data, receipt = task["outputs"]
    program = "import json,pathlib\n" + (f"assert json.loads(pathlib.Path({previous!r}).read_text())['sum'] == 55\n" if previous else "")
    program += f"pathlib.Path({data!r}).write_text(json.dumps({{'fixture':True,'sum':sum(range(1,11)),'previous':{previous!r}}}))\nprint('fixture computation complete')"
    verify = f"import json,pathlib\ndata=json.loads(pathlib.Path({data!r}).read_text())\nassert data['sum'] == 10*11//2\nassert data['previous'] == {previous!r}\nprint('independent arithmetic and continuity checks passed')"
    return [call("ask_human", question="Confirm this offline integration fixture only.", decision_kind="business_requirement"),
            call("delegate_task", assignee=workers[0], objective="Compute the declared fixture data using Python", remaining_roles=workers[1:]),
            call("run_python", code=program), {"text": "Computed fixture data with a real Python process."},
            call("delegate_task", assignee=workers[1], objective="Independently verify arithmetic and inherited data", remaining_roles=workers[2:]),
            call("run_python", code=verify), {"text": "Independent formula and continuity checks passed."},
            call("delegate_task", assignee=workers[2], objective="Read the checked data and deliver the fixture receipt", remaining_roles=[]),
            call("read_file", path=data), call("write_file", path="temporary-note.txt", content="Only this fixture-created temporary file may be removed."),
            call("delete_file", path="temporary-note.txt"),
            call("write_file", path=receipt, content="OFFLINE FIXTURE ONLY. All four roles participated. Real Python computation and independent arithmetic check completed. Human responses were scripted; original-task quality remains unverified."),
            {"text": "Delivered the checked fixture and explicit limitations."},
            {"text": "Fixture delivered; this is not formal testpack acceptance."}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a fresh output directory to preserve earlier evidence")
    pack = fixture_pack()
    providers = [ScriptedProvider(script(scene, task, scene["tasks"][0]["outputs"][0] if i else None))
                 for scene in pack["scenarios"] for i, task in enumerate(scene["tasks"])]
    service = StudioService(args.output / "studio", Settings(api_key="offline-fixture-sentinel"))
    try:
        with patch("miniagent.studio_service.DeepSeekProvider", side_effect=providers):
            report = run_pack(pack, service, args.output / "evidence",
                              answer=lambda q: "批准" if q["request_kind"] == "approval" else "Confirmed: offline fixture only.")
    finally:
        service.close()
    print(json.dumps({"machine_passed": report["machine_passed"], "formal_acceptance": False,
                      "execution_kind": "scripted_fixture", "report": str(args.output / "evidence/report.json")}, ensure_ascii=False))
    return 0 if report["machine_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
