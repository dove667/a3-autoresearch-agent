"""Only the Host operator can apply previewed repository prompt updates."""

import threading

from fastapi.testclient import TestClient
from input_source_support import deployment_source

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.inputs.versions import InputVersions

AGENT = {"Authorization": "Bearer agent"}
OPERATOR = {"Authorization": "Bearer operator"}


def setup(tmp_path, forward=None):
    root = deployment_source(tmp_path / "source")
    versions = InputVersions(root, tmp_path / "run")
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
    return root, versions, client, calls


def preview(client):
    response = client.get("/control/inputs", headers=OPERATOR)
    assert response.status_code == 200
    return response.json()["proposed"]["digest"]


def test_only_operator_can_apply_exact_preview_and_saved_context_is_preserved(tmp_path):
    source_root, versions, client, calls = setup(tmp_path)
    assert client.get("/control/inputs", headers=AGENT).status_code == 403
    (source_root / "prompt/SOUL.md").write_text("Version: 1.0.1\nUpdated research goal\n")
    digest = preview(client)
    body = {"confirm": digest}
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
    source_root, _, client, _ = setup(tmp_path)
    digest = preview(client)
    (source_root / "prompt/SOUL.md").write_text("Version: 1.0.1\nChanged after preview\n")
    assert (
        client.post("/control/inputs", headers=OPERATOR, json={"confirm": digest}).status_code
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

    source_root, versions, client, _ = setup(tmp_path, forward)
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
        (source_root / "prompt/SOUL.md").write_text("Version: 1.0.1\nUpdated question\n")
        digest = preview(client)
        assert (
            client.post(
                "/control/inputs",
                headers=OPERATOR,
                json={"confirm": digest},
            ).status_code
            == 200
        )
    finally:
        release.set()
        thread.join(5)
    assert responses[0].headers["X-A3-Input-Revision"] == before
    assert client.post("/v1/responses", headers=AGENT, json={"input": "work"}).status_code == 200
    assert "Updated question" not in received[0] and "Updated question" in received[1]


def test_removed_custom_source_is_rejected_instead_of_ignored(tmp_path):
    _, _, client, _ = setup(tmp_path)
    assert (
        client.get(
            "/control/inputs", headers=OPERATOR, params={"source_dir": "/old/task"}
        ).status_code
        == 422
    )
    digest = preview(client)
    assert (
        client.post(
            "/control/inputs",
            headers=OPERATOR,
            json={"confirm": digest, "source_dir": "/old/task"},
        ).status_code
        == 422
    )
