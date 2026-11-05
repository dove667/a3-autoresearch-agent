"""Configured 500k input windows with an independent 400k compaction trigger."""

import pytest
from fastapi.testclient import TestClient

from a3_research_agent.harness.context import compaction
from a3_research_agent.harness.context import manager as cm
from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.models.context import read_context_budget


def settings(**extra):
    return read_context_budget({"A3_MODEL_CONTEXT_TOKENS": "500000", **extra})


def test_large_context_defaults_keep_window_separate_from_trigger():
    config = settings()
    assert config["main_context_tokens"] == config["worker_context_tokens"] == 500000
    assert config["trigger_tokens"] == 400000
    assert config["target_tokens"] == 32000
    assert config["summary_input_tokens"] == 48000


def test_explicit_legacy_settings_are_not_silently_overridden():
    config = settings(A3_CONTEXT_TRIGGER_TOKENS="64000", A3_CONTEXT_TARGET_TOKENS="32000")
    assert config["trigger_tokens"] == 64000
    assert config["target_tokens"] == 32000


def test_host_publishes_headroom_without_provider_requests(tmp_path):
    client = TestClient(
        create_app(
            model_name="fake",
            sessions={"token": "main"},
            main_owner="main",
            remote_call=lambda *a: pytest.fail("remote called"),
            forward_model=lambda **kw: pytest.fail("model called"),
            usage_log=tmp_path / "usage.jsonl",
            context_budget=settings(),
        )
    )
    result = client.get("/context/config", headers={"Authorization": "Bearer token"})
    assert result.status_code == 200
    config = result.json()
    assert config["trigger_tokens"] == 400000
    assert config["decision_input_tokens"] == 441808
    assert config["main_context_tokens"] == 500000
    assert config["injected_reserve_tokens"] >= 4096


@pytest.fixture
def token_fork(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cm,
        "TOKEN_CONFIG",
        {
            "trigger_tokens": 400000,
            "target_tokens": 32000,
            "decision_input_tokens": 441808,
            "injected_reserve_tokens": 10000,
        },
    )
    monkeypatch.setattr(cm, "metric", lambda tools: 2000)
    monkeypatch.setattr(
        cm, "message_cost", lambda messages: sum(x.get("cost", 0) for x in messages)
    )
    fork = compaction.ForkCompactor(lambda *a, **k: pytest.fail("provider called"))
    started = []
    monkeypatch.setattr(
        fork, "start", lambda *a, **k: started.append(k) or {"kind": "fork_started"}
    )
    return fork, started


def test_token_soft_trigger_counts_all_reserves_and_not_byte_size(token_fork):
    fork, started = token_fork
    extra = [{"role": "user", "content": "operational", "cost": 1000}]
    # Total = messages + operational 1k + tools 2k + Host injection 10k.
    before = [{"role": "user", "content": "x" * 60000, "cost": 386999}]
    fork.before_request(before, extra_messages=extra)
    assert not started
    at_trigger = [
        {"role": "user", "content": "small bytes, explicit token fixture", "cost": 387000}
    ]
    fork.before_request(at_trigger, extra_messages=extra)
    assert len(started) == 1
    assert started[0]["budget"] == 428808


def test_hard_budget_wins_over_trigger_hysteresis(token_fork, monkeypatch):
    fork, _ = token_fork
    fork._last_after_tokens = 420000
    monkeypatch.setattr(fork, "wait", lambda *a: None)
    monkeypatch.setattr(
        cm, "fit_tool_outputs", lambda *a, **k: (_ for _ in ()).throw(cm.ContextError("cannot fit"))
    )
    with pytest.raises(cm.ContextError, match="hard decision budget"):
        fork.before_request([{"role": "user", "content": "fixture", "cost": 429809}])


def test_host_configuration_validates_separate_hard_limit(monkeypatch):
    monkeypatch.setattr(cm, "TOKEN_CONFIG", None)
    bad = {
        "enabled": True,
        "unit": "estimated_tokens",
        "tokenizer": "o200k_base",
        "trigger_tokens": 400000,
        "target_tokens": 32000,
        "summary_input_tokens": 48000,
        "injected_reserve_tokens": 10000,
        "summary_reserve_tokens": 5000,
        "decision_input_tokens": 399999,
    }
    with pytest.raises(cm.ContextError, match="budget"):
        cm.configure(lambda *a: bad)


def test_published_profile_is_explicit_and_preserves_500k_cap():
    from pathlib import Path

    from dotenv import dotenv_values

    profile = Path(__file__).resolve().parents[2] / "configs/context-400k.env.example"
    budget = read_context_budget(dotenv_values(profile))
    assert budget == settings()
    assert budget["summary_context_tokens"] == 500000


def test_worker_receives_its_own_smaller_admission_window(tmp_path):
    class Backend:
        def create(self, owner, command, files, token):
            self.token = token
            return {"session_id": owner, "status": "created"}

    backend = Backend()
    budget = settings(
        A3_WORKER_CONTEXT_TOKENS="300000",
        A3_CONTEXT_TRIGGER_TOKENS="200000",
        A3_CONTEXT_TARGET_TOKENS="128000",
    )
    client = TestClient(
        create_app(
            model_name="fake",
            sessions={"token": "main"},
            main_owner="main",
            sandbox=backend,
            remote_call=lambda *a: pytest.fail("remote called"),
            forward_model=lambda **kw: pytest.fail("model called"),
            usage_log=tmp_path / "usage.jsonl",
            context_budget=budget,
        )
    )
    result = client.post(
        "/sessions",
        headers={"Authorization": "Bearer token"},
        json={"command": ["true"], "files": {"entry.py": "pass"}},
    )
    assert result.status_code == 200
    result = client.get("/context/config", headers={"Authorization": "Bearer " + backend.token})
    assert result.status_code == 200
    assert result.json()["decision_input_tokens"] == 261808
    assert result.json()["trigger_tokens"] == 200000
