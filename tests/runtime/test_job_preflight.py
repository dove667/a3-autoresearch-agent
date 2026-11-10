import base64
import json

from a3_research_agent.harness import agent

JOB_ID = "a" * 32
PRODUCTION_ID = "b" * 32


def call(**arguments):
    return agent.dispatch_tool_call({"name": "preflight", "arguments": json.dumps(arguments)})


def test_gpu_admission_uses_same_source_and_assets_and_requires_real_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    job = {
        "gpu": True,
        "gpu_memory_mb": 6000,
        "command": ["python", "train.py", "--resume", "checkpoint.pt"],
        "files": {"train.py": "import decoder"},
        "workspace_files": [{"path": "decoder.py", "sha256": "c" * 64}],
        "input_exports": ["export-pinned"],
    }
    calls = []
    status = {"job_id": JOB_ID, "status": "completed", "exit_code": 0}
    report = {}

    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST":
            return {"job_id": PRODUCTION_ID if body["gpu"] else JOB_ID, "status": "queued"}
        if "artifacts?" in path:
            return {
                "encoding": "base64",
                "data": base64.b64encode(json.dumps(report).encode()).decode(),
            }
        return status

    monkeypatch.setattr(agent, "req", request)
    probe = call(action="check", job=job)
    assert probe["job_id"] == JOB_ID
    cpu = next(body for method, _, body in calls if method == "POST")
    assert cpu["gpu"] is False and "gpu_memory_mb" not in cpu
    assert cpu["command"] == ["env", "A3_RESEARCH_PREFLIGHT=1", *job["command"]]
    assert all(cpu[key] == job[key] for key in ("files", "workspace_files", "input_exports"))

    changed = {**job, "files": {"train.py": "import other_decoder"}}
    assert (
        call(action="submit", preflight_id=JOB_ID, job=changed)["error"]
        == "preflight_inputs_changed"
    )
    status.update(status="failed", exit_code=1)
    assert call(action="submit", preflight_id=JOB_ID, job=job)["error"] == "preflight_not_passed"
    status.update(status="completed", exit_code=0)
    assert (
        call(action="submit", preflight_id=JOB_ID, job=job)["error"] == "preflight_report_missing"
    )
    report.update(schema=1, status="passed")
    assert call(action="submit", preflight_id=JOB_ID, job=job)["job_id"] == PRODUCTION_ID
    assert call(action="submit", preflight_id=JOB_ID, job=job)["job_id"] == PRODUCTION_ID
    assert [body for method, _, body in calls if method == "POST" and body["gpu"]] == [job]
