"""One full-transcript request; test callbacks do not assess summary quality."""

import json

import pytest
from fastapi.testclient import TestClient

from a3_research_agent.harness.context import manager as cm
from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.models.context import read_context_budget


def completed():
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Retain the current goal, corrected result, original source and next action.",
                    }
                ],
            }
        ],
    }


def test_400k_transcript_is_sent_whole_once_and_replayed_without_provider(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cm,
        "TOKEN_CONFIG",
        {
            "trigger_tokens": 400000,
            "decision_input_tokens": 441808,
            "target_tokens": 32000,
            "injected_reserve_tokens": 5000,
            "summary_context_tokens": 500000,
            "summary_input_tokens": 491808,
            "summary_reserve_tokens": 5000,
        },
    )
    original = [
        {"role": "user", "content": f"Turn {index}: " + "word " * 2500} for index in range(160)
    ]
    requests = []

    def provider(method, path, body):
        assert path == "/v1/summary"
        assert set(body) == {"transcript", "source"}
        assert json.loads(body["transcript"]) == original
        assert 400000 < cm.request_cost(body) < 491808
        requests.append(body)
        return completed()

    result, record = cm.prepare(original, [], provider, budget_limit=32000)
    assert len(requests) == 1
    assert cm.message_cost(result) + 5000 <= 32000
    assert cm.paired(result)
    replay, _ = cm.prepare(
        original, [], lambda *a: pytest.fail("replayed provider request"), budget_limit=32000
    )
    assert replay == result
    assert json.loads((tmp_path / record["source"]).read_text()) == original


def test_summary_gateway_accepts_over_500k_characters_within_token_window(tmp_path):
    calls = []
    client = TestClient(
        create_app(
            model_name="fake",
            sessions={"token": "main"},
            main_owner="main",
            usage_log=tmp_path / "usage.jsonl",
            remote_call=lambda *a: {},
            context_budget=read_context_budget({"A3_MODEL_CONTEXT_TOKENS": "500000"}),
            forward_model=lambda **kw: calls.append(kw) or completed(),
        )
    )
    result = client.post(
        "/v1/summary",
        headers={"Authorization": "Bearer token"},
        json={
            "source": "context-history/snapshot-" + "a" * 64 + ".json",
            "transcript": "word " * 400000,
        },
    )
    assert result.status_code == 200
    assert len(calls) == 1
    oversized = client.post(
        "/v1/summary",
        headers={"Authorization": "Bearer token"},
        json={
            "source": "context-history/snapshot-" + "b" * 64 + ".json",
            "transcript": "word " * 500000,
        },
    )
    assert oversized.status_code == 422
    assert len(calls) == 1


def test_old_host_configuration_is_rejected_without_byte_fallback(monkeypatch):
    monkeypatch.setattr(cm, "TOKEN_CONFIG", None)
    with pytest.raises(cm.ContextError):
        cm.configure(lambda *a: {"http_error": 404})


def test_removed_summary_input_setting_is_rejected():
    with pytest.raises(ValueError, match="removed"):
        read_context_budget({"A3_SUMMARY_INPUT_TOKENS": "48000"})


def test_uncertain_dispatch_is_not_automatically_replayed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cm,
        "TOKEN_CONFIG",
        {
            "summary_input_tokens": 491808,
            "summary_reserve_tokens": 5000,
        },
    )
    original = [{"role": "user", "content": "current task and original evidence"}]
    source = cm.archive(original, "snapshot")
    calls = []

    def timeout(*args):
        calls.append(args)
        raise TimeoutError("response unknown")

    with pytest.raises(cm.ContextError, match="failed"):
        cm.full_summary(original, source, timeout)
    with pytest.raises(cm.ContextError, match="uncertain"):
        cm.full_summary(original, source, timeout)
    assert len(calls) == 1
    assert json.loads((tmp_path / source).read_text()) == original
