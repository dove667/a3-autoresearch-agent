import json

import httpx
import pytest
from fastapi.testclient import TestClient

from a3_research_agent.host.gateway.app import create_app


def client_for(tmp_path, forward=None, remote=None):
    return TestClient(
        create_app(
            model_name="private-provider/model",
            sessions={"token-a": "experiment-a", "token-b": "experiment-b"},
            forward_model=forward or (lambda **kwargs: {"id": "response", "output": []}),
            remote_call=remote or (lambda op, payload: payload),
            usage_log=tmp_path / "usage.jsonl",
        )
    )


def test_gpu_snapshot_is_authenticated_read_only_and_cached(tmp_path):
    calls = []
    snapshot = {"status": "ok", "observed_at": 123, "gpus": []}

    def remote(operation, payload):
        calls.append((operation, payload))
        return snapshot

    client = client_for(tmp_path, remote=remote)
    assert client.get("/resources/gpus").status_code == 401
    headers = {"Authorization": "Bearer token-a"}
    assert client.get("/resources/gpus", headers=headers).json() == snapshot
    assert client.get("/resources/gpus", headers=headers).json() == snapshot
    assert client.delete("/resources/gpus", headers=headers).status_code == 405
    assert calls == [("gpu-snapshot", {})]


def test_model_credentials_and_selection_stay_on_host(tmp_path):
    requests = []
    client = client_for(tmp_path, forward=lambda **kw: requests.append(kw) or {"output": []})
    response = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer token-a"},
        json={"model": "container-alias", "input": "probe", "tools": []},
    )
    assert response.status_code == 200
    assert requests[0]["model"] == "private-provider/model"
    assert "api_key" not in requests[0]
    assert client.post("/v1/responses", json={"input": "probe"}).status_code == 401


def test_debug_events_are_separate_from_model_execution_records(tmp_path):
    trajectory = tmp_path / "trajectories/experiment"
    trajectory.mkdir(parents=True)
    logs = tmp_path / "runs/experiment"
    app = create_app(
        model_name="configured/model",
        sessions={"token": "research"},
        forward_model=lambda **kwargs: {"id": "response", "output": []},
        remote_call=lambda operation, payload: payload,
        usage_log=trajectory / "model-usage.jsonl",
        debug_root=logs,
    )
    response = TestClient(app).post(
        "/v1/responses",
        headers={"Authorization": "Bearer token"},
        json={"input": "Inspect the supplied evidence"},
    )
    assert response.status_code == 200
    assert (trajectory / "model-usage.jsonl").is_file()
    assert (logs / "gateway-events.jsonl").is_file()
    assert not (trajectory / "gateway-events.jsonl").exists()
    assert not (logs / "model-usage.jsonl").exists()


@pytest.mark.parametrize("upstream_status", [400, 429, 502])
def test_gateway_records_upstream_http_status_without_provider_details(tmp_path, upstream_status):
    def forward(**_):
        request = httpx.Request("POST", "https://provider.example/responses")
        response = httpx.Response(upstream_status, request=request, text="private provider detail")
        response.raise_for_status()

    response = client_for(tmp_path, forward=forward).post(
        "/v1/responses", headers={"Authorization": "Bearer token-a"}, json={"input": "probe"}
    )
    assert response.status_code == upstream_status
    records = (tmp_path / "logs/gateway-events.jsonl").read_text()
    assert json.loads(records.splitlines()[-1])["upstream_status"] == upstream_status
    assert "private provider detail" not in records + response.text


@pytest.mark.parametrize("direct", [False, True])
def test_upstream_bad_request_records_only_safe_validation_details(tmp_path, direct):
    from a3_research_agent.host.models.direct import DirectHTTPError

    provider_id = "b9b397f0-f522-45b2-a4dd-5a8927316f2f"
    proxy_id = "fbc79f50-1378-49ad-a08c-2a670cfda134"
    headers = {
        "llm_provider-x-request-id": provider_id,
        "x-litellm-call-id": proxy_id,
        "x-trace-id": "sk-private-trace-credential",
    }
    payload = {
        "error": {
            "type": "invalid_request_error",
            "code": "unknown_parameter",
            "param": "tools[4].parameters",
            "message": "Unsupported parameter tools[4].parameters. Echo: sk-secret-message",
            "extra": "private payload",
        }
    }

    def forward(**_):
        if direct:
            raise DirectHTTPError(400, json.dumps(payload), headers)
        request = httpx.Request("POST", "https://provider.example/responses")
        httpx.Response(400, request=request, json=payload, headers=headers).raise_for_status()

    response = client_for(tmp_path, forward=forward).post(
        "/v1/responses",
        headers={"Authorization": "Bearer token-a"},
        json={"input": "private input"},
    )
    assert response.status_code == 400
    assert response.headers["X-A3-Model-Error"] == "upstream_bad_request"
    records = (tmp_path / "logs/gateway-events.jsonl").read_text()
    event = json.loads(records.splitlines()[-1])
    assert event["request_id"] == response.headers["X-Request-ID"]
    (detail,) = event["upstream_validation_errors"]
    assert detail.pop("request_shape")["input_items"] == 1
    assert detail == {
        "request_id": provider_id,
        "proxy_call_id": proxy_id,
        "type": "invalid_request_error",
        "code": "unknown_parameter",
        "param": "tools[4].parameters",
        "reasons": ["unsupported_parameter"],
    }
    for private in ("sk-secret-message", "private payload", "private input", "sk-private-trace"):
        assert private not in records + response.text
    assert provider_id not in response.text


def test_model_request_cannot_override_upstream_or_use_hosted_tools(tmp_path):
    client = client_for(tmp_path)
    for extra in (
        {"api_base": "http://other"},
        {"api_key": "other"},
        {"tools": [{"type": "web_search"}]},
    ):
        response = client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer token-a"},
            json={"input": "probe", **extra},
        )
        assert response.status_code == 422


def test_job_owner_is_injected_and_cannot_be_supplied_by_container(tmp_path):
    calls = []
    client = client_for(
        tmp_path, remote=lambda op, payload: calls.append((op, payload)) or {"job_id": "j"}
    )
    headers = {"Authorization": "Bearer token-a"}
    assert (
        client.post("/jobs", headers=headers, json={"command": ["python", "-V"]}).status_code == 200
    )
    assert calls == [
        (
            "submit",
            {
                "command": ["python", "-V"],
                "gpu": False,
                "files": {},
                "experiment_id": "experiment-a",
                "job_id": calls[0][1]["job_id"],
            },
        )
    ]
    assert (
        client.post(
            "/jobs", headers=headers, json={"command": ["true"], "experiment_id": "experiment-b"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/jobs", headers=headers, json={"command": ["true"], "mounts": ["/"]}
        ).status_code
        == 422
    )


def test_status_cancel_and_artifact_all_carry_authenticated_owner(tmp_path):
    calls = []
    client = client_for(tmp_path, remote=lambda op, payload: calls.append((op, payload)) or {})
    headers = {"Authorization": "Bearer token-b"}
    assert client.get("/jobs/j", headers=headers).status_code == 200
    assert client.delete("/jobs/j", headers=headers).status_code == 200
    assert (
        client.get("/jobs/j/artifacts", params={"path": "camera.png"}, headers=headers).status_code
        == 200
    )
    assert [call[0] for call in calls] == ["status", "cancel", "artifact"]
    assert all(call[1]["experiment_id"] == "experiment-b" for call in calls)


def test_provider_errors_do_not_expose_credentials(tmp_path):
    def failing(**kwargs):
        raise RuntimeError("PRIVATE_CREDENTIAL")

    client = client_for(tmp_path, forward=failing)
    response = client.post(
        "/v1/responses", headers={"Authorization": "Bearer token-a"}, json={"input": "probe"}
    )
    assert response.status_code == 502
    assert "PRIVATE_CREDENTIAL" not in response.text


def test_submission_timeout_returns_pre_registered_job_id(tmp_path):
    submitted = []

    def remote(operation, payload):
        if operation == "submit":
            submitted.append(payload)
            raise TimeoutError("lost SSH response after admission")
        return {"job_id": payload["job_id"], "status": "running"}

    client = client_for(tmp_path, remote=remote)
    headers = {"Authorization": "Bearer token-a"}
    response = client.post("/jobs", headers=headers, json={"command": ["true"]})
    assert response.status_code == 502
    receipt = response.json()["detail"]
    assert receipt["code"] == "submission_unknown"
    assert receipt["job_id"] == submitted[0]["job_id"]
    assert len(receipt["job_id"]) == 32
    assert client.get(receipt["status_path"], headers=headers).json()["status"] == "running"
    assert len(submitted) == 1


def test_job_list_recovers_owned_history_without_new_submissions(tmp_path):
    calls = []

    def remote(operation, payload):
        calls.append((operation, payload))
        return {"jobs": [{"job_id": "a" * 32}], "next_after": None}

    client = client_for(tmp_path, remote=remote)
    headers = {"Authorization": "Bearer token-a"}
    result = client.get("/jobs?limit=10", headers=headers)
    assert result.status_code == 200
    assert result.json()["status_source"] == "controller_metadata"
    assert calls == [("jobs-list", {"experiment_id": "experiment-a", "after": "", "limit": 10})]
    assert client.get("/jobs?scope=outsider", headers=headers).status_code == 403
    assert len(calls) == 1
