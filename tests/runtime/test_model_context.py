import json

import pytest
from fastapi.testclient import TestClient

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.inputs.feedback import HumanFeedback


def test_context_configuration_counts_injected_input_without_model_call(tmp_path):
    calls = []
    source = tmp_path / "feedback.md"
    source.write_text("Check the actual saved episodes. " * 1900)
    feedback = HumanFeedback(tmp_path / "run")
    feedback.register(source, "main")
    client = TestClient(
        create_app(
            model_name="fixed/main",
            sessions={"token": "main"},
            main_owner="main",
            task_instructions="host phase rules " * 6000,
            human_feedback=feedback,
            usage_log=tmp_path / "usage.jsonl",
            remote_call=lambda *args: {},
            forward_model=lambda **kw: calls.append(kw) or {"output": []},
            context_budget={
                "main_context_tokens": 500000,
                "worker_context_tokens": 500000,
                "summary_context_tokens": 500000,
                "trigger_tokens": 200000,
                "target_tokens": 32000,
            },
        )
    )
    result = client.get("/context/config", headers={"Authorization": "Bearer token"})
    assert result.status_code == 200
    config = result.json()
    assert config["unit"] == "estimated_tokens" and config["tokenizer"] == "o200k_base"
    assert config["injected_reserve_tokens"] > 32000
    assert config["trigger_tokens"] == 200000
    assert config["target_tokens"] == 32000
    from a3_research_agent.harness.context import manager as context

    with pytest.raises(context.ContextError, match="budget"):
        context.configure(lambda *args: config)
    assert calls == []
    assert client.get("/context/config").status_code == 401


def test_token_counter_is_encoding_count_not_utf8_bytes():
    from a3_research_agent.token_count import count_tokens

    text = "保留纠正后的事实和未知结果。"
    assert count_tokens(text) == 10
    assert count_tokens(text) < len(text.encode("utf-8"))


def test_context_budget_rejects_threshold_without_model_window_headroom(tmp_path):
    with pytest.raises(ValueError):
        create_app(
            model_name="fixed/main",
            sessions={},
            usage_log=tmp_path / "usage.jsonl",
            remote_call=lambda *args: {},
            forward_model=lambda **kw: {},
            context_budget={
                "main_context_tokens": 4000,
                "worker_context_tokens": 4000,
                "summary_context_tokens": 4000,
                "trigger_tokens": 64000,
                "target_tokens": 32000,
            },
        )


def test_output_reserve_can_be_lowered_for_a_slow_provider(monkeypatch):
    from a3_research_agent.host.models.context import read_context_budget

    for key in (
        "A3_MODEL_CONTEXT_TOKENS",
        "A3_WORKER_CONTEXT_TOKENS",
        "A3_SUMMARY_CONTEXT_TOKENS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("A3_OUTPUT_RESERVE_TOKENS", "2048")
    budget = read_context_budget(
        {
            "A3_MODEL_CONTEXT_TOKENS": "262144",
            "A3_WORKER_CONTEXT_TOKENS": "262144",
            "A3_SUMMARY_CONTEXT_TOKENS": "524288",
            "A3_CONTEXT_TRIGGER_TOKENS": "196608",
            "A3_CONTEXT_TARGET_TOKENS": "32768",
            "A3_OUTPUT_RESERVE_TOKENS": "2048",
        }
    )
    assert budget["output_reserve_tokens"] == 2048


def test_responses_upstream_uses_local_token_admission(tmp_path):
    calls = []

    def upstream(**request):
        calls.append(request)
        return {"status": "completed", "output": [], "usage": {"input_tokens": 55}}

    log = tmp_path / "usage.jsonl"
    client = TestClient(
        create_app(
            model_name="fixed/main",
            sessions={"token": "main"},
            usage_log=log,
            remote_call=lambda *args: {},
            forward_model=upstream,
            context_budget={
                "main_context_tokens": 65536,
                "worker_context_tokens": 65536,
                "summary_context_tokens": 65536,
                "trigger_tokens": 16000,
                "target_tokens": 8000,
            },
        )
    )
    headers = {"Authorization": "Bearer token"}
    first = client.post(
        "/v1/responses", headers=headers, json={"input": [{"role": "user", "content": "hello"}]}
    )
    assert first.json()["usage"]["input_tokens"] == 55
    assert json.loads(log.read_text())["estimated_input_tokens"] > 55
    request = {"input": [{"role": "user", "content": "word " * 70000}]}
    second = client.post("/v1/responses", headers=headers, json=request)
    assert second.status_code == 422
    assert len(calls) == 1
