"""Only the Host operator can apply a previewed task-input replacement."""

import threading
from pathlib import Path

from fastapi.testclient import TestClient
from task_input_support import task_inputs

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.inputs.versions import InputVersions

ROOT = Path(__file__).resolve().parents[2]
AGENT = {"Authorization": "Bearer agent"}
OPERATOR = {"Authorization": "Bearer operator"}


def setup(tmp_path, forward=None):
    task = task_inputs(tmp_path / "task")
    versions = InputVersions(ROOT, tmp_path / "run", custom_dir=task)
    versions.activate(versions.prepare(), "start")
    calls = []
    client = TestClient(
        create_app(
            model_name="fixture/model",
            sessions={"agent": "main"},
            main_owner="main",
            forward_model=forward or (lambda **kwargs: calls.append(kwargs) or {"output": []}),
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
            input_versions=versions,
            operator_token="operator",
        )
    )
    return task, versions, client, calls


def preview(client, task):
    response = client.get("/control/inputs", headers=OPERATOR, params={"source_dir": str(task)})
    assert response.status_code == 200
    return response.json()["proposed"]["digest"]


def test_only_operator_can_apply_exact_preview_and_saved_context_is_preserved(tmp_path):
    task, versions, client, calls = setup(tmp_path)
    assert client.get("/control/inputs", headers=AGENT).status_code == 403
    (task / "goal.md").write_text("Version: 1.0.1\nUpdated research goal\n")
    digest = preview(client, task)
    body = {"confirm": digest, "source_dir": str(task)}
    assert client.post("/control/inputs", headers=AGENT, json=body).status_code == 403
    assert (
        client.post(
            "/control/inputs", headers=OPERATOR, json={**body, "confirm": "0" * 64}
        ).status_code
        == 409
    )
    assert client.post("/control/inputs", headers=OPERATOR, json=body).status_code == 200
    context = [{"role": "user", "content": "Saved observations"}]
    response = client.post("/v1/responses", headers=AGENT, json={"input": context})
    assert response.status_code == 200
    assert calls[-1]["input"] == context
    assert "Updated research goal" in calls[-1]["instructions"]
    assert response.headers["X-A3-Input-Revision"] == versions.manifest["revision"]


def test_mutation_after_preview_invalidates_confirmation(tmp_path):
    task, _, client, _ = setup(tmp_path)
    digest = preview(client, task)
    (task / "goal.md").write_text("Version: 1.0.1\nChanged after preview\n")
    assert (
        client.post(
            "/control/inputs", headers=OPERATOR, json={"confirm": digest, "source_dir": str(task)}
        ).status_code
        == 409
    )


def test_inflight_request_keeps_original_snapshot_and_next_call_gets_update(tmp_path):
    entered, release = threading.Event(), threading.Event()
    received = []

    def forward(**kwargs):
        received.append(kwargs["instructions"])
        if len(received) == 1:
            entered.set()
            assert release.wait(5)
        return {"output": []}

    task, versions, client, _ = setup(tmp_path, forward)
    responses = []
    thread = threading.Thread(
        target=lambda: responses.append(
            client.post("/v1/responses", headers=AGENT, json={"input": "work"})
        )
    )
    thread.start()
    assert entered.wait(5)
    before = versions.manifest["revision"]
    try:
        (task / "goal.md").write_text("Version: 1.0.1\nUpdated question\n")
        digest = preview(client, task)
        assert (
            client.post(
                "/control/inputs",
                headers=OPERATOR,
                json={"confirm": digest, "source_dir": str(task)},
            ).status_code
            == 200
        )
    finally:
        release.set()
        thread.join(5)
    assert responses[0].headers["X-A3-Input-Revision"] == before
    assert client.post("/v1/responses", headers=AGENT, json={"input": "work"}).status_code == 200
    assert "Updated question" not in received[0] and "Updated question" in received[1]
