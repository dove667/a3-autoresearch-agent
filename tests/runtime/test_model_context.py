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
            phase_instructions="host phase rules " * 6000,
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
    assert config["target_tokens"] - config["injected_reserve_tokens"] == 32000
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
