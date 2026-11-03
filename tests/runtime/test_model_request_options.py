import json

import pytest
from fastapi.testclient import TestClient

from runtime.local.host_gateway import create_app


def test_operator_options_apply_to_decisions_summaries_and_workers(tmp_path):
    requests = []

    def forward(**kwargs):
        requests.append(kwargs)
        return {
            "model": "observed-model",
            "reasoning": {"effort": "medium"},
            "service_tier": "default",
            "output": [],
        }

    app = create_app(
        model_name="configured-provider/model",
        sessions={"main-token": "main", "worker-token": "worker"},
        forward_model=forward,
        remote_call=lambda *args: {},
        usage_log=tmp_path / "usage.jsonl",
        model_request_options={"reasoning": {"effort": "medium"}, "service_tier": "default"},
    )
    client = TestClient(app)
    for token, body in [
        ("main-token", {"input": [], "tools": [], "reasoning": {"effort": "high"}}),
        ("main-token", {"input": []}),
        ("worker-token", {"input": [], "tools": []}),
    ]:
        assert (
            client.post(
                "/v1/responses", headers={"Authorization": "Bearer " + token}, json=body
            ).status_code
            == 200
        )
    assert len(requests) == 3
    assert all(
        r["model"] == "configured-provider/model"
        and r["reasoning"] == {"effort": "medium"}
        and r["service_tier"] == "default"
        for r in requests
    )
    logs = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert all(
        r["model_request_options"] == {"reasoning": {"effort": "medium"}, "service_tier": "default"}
        for r in logs
    )
    assert all(
        r["response_model"] == "observed-model"
        and r["response_reasoning_effort"] == "medium"
        and r["response_service_tier"] == "default"
        for r in logs
    )


@pytest.mark.parametrize(
    "unsafe",
    [
        {"api_key": "not-allowed"},
        {"api_base": "https://elsewhere.invalid"},
        {"service_tier": "priority"},
        {"reasoning": {"effort": []}},
    ],
)
def test_operator_options_cannot_configure_credentials_routes_or_faster_tiers(tmp_path, unsafe):
    with pytest.raises(ValueError):
        create_app(
            model_name="configured/model",
            sessions={},
            forward_model=lambda **kwargs: {},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
            model_request_options=unsafe,
        )


def test_absent_policy_preserves_existing_client_reasoning(tmp_path):
    calls = []
    client = TestClient(
        create_app(
            model_name="configured/model",
            sessions={"token": "owner"},
            forward_model=lambda **kwargs: calls.append(kwargs) or {"output": []},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
        )
    )
    assert (
        client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer token"},
            json={"input": [], "reasoning": {"effort": "low"}},
        ).status_code
        == 200
    )
    assert calls[0]["reasoning"] == {"effort": "low"} and "service_tier" not in calls[0]
