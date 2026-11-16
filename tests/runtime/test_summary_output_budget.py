from a3_research_agent.harness import state_paths
from tests.runtime.summary_fixtures import handoff_from_request

"""A single full-history summary gets enough output space to complete."""

import json

import pytest
from fastapi.testclient import TestClient

from a3_research_agent.harness.context import manager as cm
from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.models.context import read_context_budget


def summary_client(tmp_path, output_reserve, calls):
    budget = read_context_budget({"A3_MODEL_CONTEXT_TOKENS": "500000"})
    budget["output_reserve_tokens"] = output_reserve

    def provider(**arguments):
        calls.append(arguments)
        enough = arguments["max_output_tokens"] >= 1536
        payload = json.loads(arguments["input"][0]["content"])
        text = "unused transport-only response"
        if payload["transcript"].startswith("["):
            text = handoff_from_request(
                payload,
                "Current goal, verified experiment and code references; continue the pending run.",
            )
        return {
            "status": "completed" if enough else "incomplete",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": text,
                        }
                    ],
                }
            ],
            "usage": {"output_tokens": 1536 if enough else arguments["max_output_tokens"]},
        }

    return TestClient(
        create_app(
            model_name="fake",
            sessions={"token": "main"},
            main_owner="main",
            usage_log=tmp_path / "usage.jsonl",
            remote_call=lambda *args: {},
            context_budget=budget,
            forward_model=provider,
        )
    )


@pytest.mark.parametrize("output_reserve", [8192, 2048])
def test_summary_can_complete_beyond_one_thousand_tokens(tmp_path, monkeypatch, output_reserve):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cm,
        "TOKEN_CONFIG",
        {
            "summary_input_tokens": 500000 - output_reserve,
            "summary_reserve_tokens": 5000,
            "target_tokens": 32000,
        },
    )
    calls = []
    client = summary_client(tmp_path, output_reserve, calls)
    original = [
        {"role": "user", "content": "Research transcript, source evidence and next actions"}
    ]
    source = cm.archive(original, "snapshot")

    def request(method, path, body):
        response = client.request(
            method, path, json=body, headers={"Authorization": "Bearer token"}
        )
        assert response.status_code == 200
        return response.json()

    text, usage, _ = cm.full_summary(original, source, request)
    assert "pending run" in text
    assert usage["output_tokens"] == 1536
    assert len(calls) == 1
    assert 1536 <= calls[0]["max_output_tokens"] <= 32000
    assert json.loads(calls[0]["input"][0]["content"])["transcript"] == json.dumps(original)
    assert json.loads(state_paths.path(source).read_text()) == original


def test_small_default_reserve_does_not_cap_summary_generation(tmp_path):
    calls = []
    client = summary_client(tmp_path, 512, calls)
    response = client.post(
        "/v1/summary",
        headers={"Authorization": "Bearer token"},
        json={"source": "context-history/snapshot-" + "a" * 64 + ".json", "transcript": "data"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["usage"]["output_tokens"] == 1536
    assert calls[0]["max_output_tokens"] == 32000
