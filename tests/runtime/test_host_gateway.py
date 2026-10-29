from fastapi.testclient import TestClient

from runtime.local.host_gateway import create_app


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
