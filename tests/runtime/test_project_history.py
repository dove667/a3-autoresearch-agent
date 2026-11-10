"""Project job access uses trusted Host records and keeps lifecycle checks."""

import json

import pytest
from fastapi.testclient import TestClient

from a3_research_agent.host.gateway.app import create_app


def test_current_agent_can_reuse_a_historical_job_and_export(tmp_path):
    current = tmp_path / "run-current"
    previous = tmp_path / "run-previous"
    private = previous / ".host"
    private.mkdir(parents=True)
    historical = "a" * 32
    outside = "b" * 32
    (private / "state.json").write_text(
        json.dumps(
            {
                "run_id": previous.name,
                "gateway": {
                    "root_owners": [previous.name],
                    "children": {"old-agent": {"owner": "old-agent"}},
                    "jobs": {},
                },
            }
        )
    )
    readers = {"old-agent"}

    def remote(operation, request):
        actor = request["experiment_id"]
        if operation in {"jobs-status", "status", "cancel", "artifact", "export-create"}:
            ids = request.get("job_ids", [request.get("job_id")])
            if ids != [historical] or actor != "old-agent":
                return {"status": "error", "error": "PermissionError"}
            assert operation != "cancel"
            return {"status": "completed", "area": "outputs", "job_id": historical}
        if operation == "jobs-list":
            assert (actor, request["run_id"]) == ("old-agent", previous.name)
            return {"jobs": [{"job_id": historical}]}
        if operation in {"exports-check", "export-status"}:
            if actor not in readers:
                return {"status": "error", "error": "PermissionError"}
            return {"status": "ready", "owner": "old-agent"}
        if operation == "export-grant":
            assert actor == "old-agent"
            readers.add(request["recipient"])
            return {"access": "read_only"}
        if operation == "submit":
            assert actor == "main" and actor in readers
            return {"status": "queued", "job_id": request["job_id"]}
        pytest.fail(f"Unexpected controller operation {operation}")

    client = TestClient(
        create_app(
            model_name="test",
            sessions={"main-token": "main"},
            main_owner="main",
            forward_model=lambda **kwargs: pytest.fail("No model call expected"),
            remote_call=remote,
            usage_log=current / "model-usage.jsonl",
            project_history_run=current,
        )
    )
    headers = {"Authorization": "Bearer main-token"}
    assert client.get(f"/jobs/{historical}", headers=headers).status_code == 200
    assert client.get(f"/jobs/{outside}", headers=headers).status_code == 403
    assert client.get("/jobs", params={"scope": "old-agent"}, headers=headers).json()["jobs"]
    assert (
        client.get(
            f"/jobs/{historical}/artifacts",
            params={"path": "report.json", "area": "outputs"},
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/jobs/{historical}/exports", headers=headers, json={"path": "checkpoint.pt"}
        ).status_code
        == 200
    )
    assert client.delete(f"/jobs/{historical}", headers=headers).status_code == 403
    assert (
        client.post(
            "/jobs",
            headers=headers,
            json={"command": ["true"], "input_exports": ["export-history"]},
        ).status_code
        == 200
    )
