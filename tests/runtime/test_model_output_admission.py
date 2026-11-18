"""All model routes admit both input and the actual requested output allowance."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.models.context import read_context_budget
from a3_research_agent.token_count import estimate_request_tokens


def configured(tmp_path, **budget_overrides):
    calls = []
    sessions = {"main-token": "main", "worker-token": "worker"}
    budget = read_context_budget({"A3_MODEL_CONTEXT_TOKENS": "500000"})
    budget["output_reserve_tokens"] = 10240
    budget.update(budget_overrides)
    app = create_app(
        model_name="fake/main",
        main_owner="main",
        sessions=sessions,
        usage_log=tmp_path / "usage.jsonl",
        remote_call=lambda *args: {},
        context_budget=budget,
        restored_state={
            "sessions": dict(sessions),
            "root_owners": ["main"],
            "session_records": {},
            "scope_parents": {},
            "jobs": {},
            "main_owner": "main",
        },
        forward_model=lambda **kwargs: (
            calls.append(kwargs) or {"status": "completed", "output": []}
        ),
    )
    gateway = next(
        route.endpoint.__self__
        for route in app.routes
        if getattr(route, "path", None) == "/v1/responses"
    )
    return TestClient(app), gateway, calls


@pytest.mark.parametrize("actor", ["main", "worker"])
def test_requested_output_that_exhausts_window_is_rejected_before_provider(tmp_path, actor):
    client, _, calls = configured(tmp_path)
    response = client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {actor}-token"},
        json={"input": "tiny input", "max_output_tokens": 500000},
    )
    assert response.status_code == 422
    assert not calls


def test_vision_route_counts_output_and_image_reserve_together(tmp_path):
    _, gateway, calls = configured(tmp_path)
    with pytest.raises(HTTPException) as error:
        gateway.forward_response(
            {"input": "tiny input", "max_output_tokens": 500000},
            "Bearer main-token",
            purpose="vision",
            vision_image_reserve=32768,
        )
    assert error.value.status_code == 422
    assert not calls


@pytest.mark.parametrize("actor", ["main", "worker"])
def test_missing_output_allowance_gets_explicit_configured_default(tmp_path, actor):
    client, _, calls = configured(tmp_path)
    response = client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {actor}-token"},
        json={"input": "tiny input"},
    )
    assert response.status_code == 200
    assert calls[0]["max_output_tokens"] == 10240


def test_vision_internal_default_uses_same_explicit_reserve(tmp_path):
    _, gateway, calls = configured(tmp_path)
    gateway.forward_response({"input": "tiny input"}, "Bearer main-token", purpose="vision")
    assert calls[0]["max_output_tokens"] == 10240


@pytest.mark.parametrize("output", [True, -1, 0, 3.5, "10000"])
def test_invalid_output_allowance_is_rejected_without_provider(tmp_path, output):
    client, _, calls = configured(tmp_path)
    response = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer main-token"},
        json={"input": "tiny input", "max_output_tokens": output},
    )
    assert response.status_code == 422
    assert not calls


def test_valid_large_output_remains_explicit_and_inside_headroom(tmp_path):
    client, _, calls = configured(tmp_path)
    response = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer main-token"},
        json={"input": "tiny input", "max_output_tokens": 400000},
    )
    assert response.status_code == 200
    arguments = {key: value for key, value in calls[0].items() if key != "model"}
    assert arguments["max_output_tokens"] == 400000
    assert estimate_request_tokens(arguments) + arguments["max_output_tokens"] <= 450000


@pytest.mark.parametrize(
    ("actor", "purpose", "maximum"),
    [
        ("main", "decision", 1048576),
        ("worker", "decision", 262144),
        ("main", "vision", 262144),
        ("main", "summary", 524288),
    ],
)
def test_configured_model_limits_take_precedence_over_request_allowance(
    tmp_path, actor, purpose, maximum
):
    _, gateway, calls = configured(
        tmp_path,
        main_max_output_tokens=1048576,
        worker_max_output_tokens=262144,
        summary_max_output_tokens=524288,
    )
    gateway.forward_response(
        {"input": "tiny input", "max_output_tokens": 2048},
        f"Bearer {actor}-token",
        purpose=purpose,
    )
    assert calls[0]["max_output_tokens"] == maximum


def test_model_output_maximum_inherits_across_roles():
    from a3_research_agent.host.models.context import ContextBudget

    config = read_context_budget({"A3_MODEL_MAX_OUTPUT_TOKENS": "1048576"})
    budget = ContextBudget(**config)
    assert budget.maximum_output_tokens("main") == 1048576
    assert budget.maximum_output_tokens("worker") == 1048576
    assert budget.maximum_output_tokens("summary") == 1048576


def test_summary_output_maximum_changes_the_route_identity(tmp_path):
    original, _, _ = configured(tmp_path)
    updated, _, _ = configured(tmp_path, main_max_output_tokens=1048576)
    headers = {"Authorization": "Bearer main-token"}
    before = original.get("/context/config", headers=headers).json()
    after = updated.get("/context/config", headers=headers).json()
    assert after["summary_model_max_output_tokens"] == 1048576
    assert after["model_routes_sha256"] != before["model_routes_sha256"]
    assert after["decision_input_tokens"] == before["decision_input_tokens"]
