"""The model uses one prepared request without repeating a remote submission."""

import base64
import json

from a3_research_agent.harness import agent


def test_public_gpu_workflow_preserves_unknown_job_identity_and_never_replays(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    for name, path in {
        "A3_WORKSPACE": workspace,
        "A3_INPUT_ROOT": tmp_path / "inputs",
        "A3_REFERENCE_ROOT": tmp_path / "reference",
    }.items():
        monkeypatch.setenv(name, str(path))
    cpu, gpu, sends = "a" * 32, "b" * 32, []

    def request(method, path, data=None, **options):
        if method == "POST" and path == "/jobs":
            sends.append(data)
            if data["gpu"]:
                return {"http_error": 502, "body": json.dumps({"detail": {"job_id": gpu}})}
            return {"job_id": cpu, "status": "queued"}
        if path == "/jobs/wait":
            assert data["job_ids"] == [cpu]
            return {"jobs": [{"job_id": cpu, "status": "completed", "exit_code": 0}]}
        if path == "/jobs/" + cpu:
            return {"job_id": cpu, "status": "completed", "exit_code": 0}
        if "/artifacts?" in path:
            return {
                "encoding": "base64",
                "data": base64.b64encode(b'{"schema":1,"status":"passed"}').decode(),
            }
        raise AssertionError((method, path))

    monkeypatch.setattr(agent, "req", request)

    def call(name, **args):
        return agent.dispatch_tool_call({"name": name, "arguments": json.dumps(args)})

    assert call("write", path="scripts/train.py", content="print('training')")["ok"]
    assert call("write", path="experiments/trial/config.json", content='{"steps":10}')["ok"]
    prepared = call(
        "job_prepare",
        command=["python", "scripts/train.py"],
        attachments=[{"path": "scripts/train.py"}, {"path": "experiments/trial/config.json"}],
    )
    assert prepared["ok"] and prepared["preflight_job_id"] == cpu
    assert call("job_wait", job_ids=[cpu])["ok"]
    result = call("job_submit", prepared_id=prepared["prepared_id"])
    assert (
        result["ok"] is False
        and result["job_id"] == gpu
        and result["error"] == "submission_unknown"
    )
    assert "body" not in result
    assert call("job_submit", prepared_id=prepared["prepared_id"]) == result
    assert len(sends) == 2
    assert sends[0]["workspace_files"] == sends[1]["workspace_files"]
