import json

from fastapi.testclient import TestClient
from host_fixtures import host_create_session

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.sessions.workspace import SessionCapacityError


def test_diagnostic_log_correlates_errors_without_saving_payload_or_exception(tmp_path):
    def failing(**kwargs):
        raise RuntimeError("PRIVATE_PROVIDER_SECRET")

    client = TestClient(
        create_app(
            model_name="provider/model",
            sessions={"PRIVATE_AUTH_TOKEN": "a"},
            forward_model=failing,
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
        )
    )
    response = client.post(
        "/v1/responses",
        json={"input": "PRIVATE_PROMPT"},
        headers={"Authorization": "Bearer PRIVATE_AUTH_TOKEN"},
    )
    assert response.status_code == 502
    text = (tmp_path / "logs/gateway-events.jsonl").read_text()
    events = [json.loads(line) for line in text.splitlines()]
    assert [event["event"] for event in events] == ["started", "finished"]
    assert events[-1]["request_id"] == response.headers["X-Request-ID"]
    assert events[-1]["error_layer"] == "model_provider"
    assert events[-1]["error_type"] == "RuntimeError"
    assert events[-1]["status_code"] == 502
    assert all(
        secret not in text + response.text
        for secret in (
            "PRIVATE_PROVIDER_SECRET",
            "PRIVATE_AUTH_TOKEN",
            "PRIVATE_PROMPT",
        )
    )


def test_busy_host_session_returns_actionable_retry(tmp_path):
    class BusySandbox:
        def check_capacity(self):
            raise SessionCapacityError("private detail")

    client = TestClient(
        create_app(
            model_name="provider/model",
            sessions={"token": "a"},
            forward_model=lambda **kw: {},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
            session_runtime=BusySandbox(),
        )
    )
    response = host_create_session(
        client, headers={"Authorization": "Bearer token"}, json={"command": ["true"]}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["max_active"] == 1
    assert response.json()["detail"]["retry_after_seconds"] == 2


def test_comparison_check_requires_identity_and_returns_condition_differences(tmp_path):
    client = TestClient(
        create_app(
            model_name="provider/model",
            sessions={"token": "a"},
            forward_model=lambda **kw: {},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
        )
    )
    assert (
        client.post("/checks/comparison", json={"baseline": {}, "candidate": {}}).status_code == 401
    )
    response = client.post(
        "/checks/comparison",
        headers={"Authorization": "Bearer token"},
        json={"baseline": {}, "candidate": {}},
    )
    assert response.status_code == 200
    assert not response.json()["comparable"]
