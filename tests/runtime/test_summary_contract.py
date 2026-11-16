"""Compaction needs a complete summary inside the whole request's token target."""

import json

import pytest
from fastapi.testclient import TestClient

from a3_research_agent.harness import state_paths
from a3_research_agent.harness.context import compaction
from a3_research_agent.harness.context import manager as cm
from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.models.context import read_context_budget
from a3_research_agent.token_count import count_tokens, estimate_request_tokens
from tests.runtime.summary_fixtures import handoff_from_request


def completed(text):
    return {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


def host(tmp_path, provider, **settings):
    return TestClient(
        create_app(
            model_name="fake",
            sessions={"token": "main"},
            main_owner="main",
            usage_log=tmp_path / "usage.jsonl",
            remote_call=lambda *args, **kwargs: {},
            context_budget=read_context_budget({"A3_MODEL_CONTEXT_TOKENS": "500000", **settings}),
            forward_model=provider,
        )
    )


def endpoint(client):
    def request(method, path, body=None, **kwargs):
        response = client.request(
            method, path, json=body, headers={"Authorization": "Bearer token"}
        )
        return (
            response.json() if response.status_code < 400 else {"http_error": response.status_code}
        )

    return request


def local_config(monkeypatch):
    monkeypatch.setattr(
        cm,
        "TOKEN_CONFIG",
        {
            "decision_input_tokens": 90000,
            "trigger_tokens": 40000,
            "target_tokens": 32000,
            "injected_reserve_tokens": 6000,
            "summary_context_tokens": 500000,
            "summary_input_tokens": 491808,
            "summary_reserve_tokens": 5000,
        },
    )
    monkeypatch.setattr(cm.time, "sleep", lambda seconds: None)


def history():
    return [
        {"role": "user", "content": "scientific observations " * 25000},
        {"type": "function_call", "call_id": "fresh", "name": "read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "fresh", "output": "latest original result"},
    ]


def test_host_target_is_total_context_without_adding_injected_reserves(tmp_path):
    client = host(tmp_path, lambda **kwargs: pytest.fail("unexpected model request"))
    config = endpoint(client)("GET", "/context/config")
    assert config["target_tokens"] == 32000
    assert config["injected_reserve_tokens"] > 0


@pytest.mark.parametrize("requested,allowed", [(20000, 20000), (2000000, None)])
def test_host_uses_requested_summary_budget_beyond_old_constant(tmp_path, requested, allowed):
    calls = []
    client = host(
        tmp_path, lambda **kwargs: calls.append(kwargs) or completed("Complete research state")
    )
    result = endpoint(client)(
        "POST",
        "/v1/summary",
        {
            "source": "context-history/snapshot-" + "a" * 64 + ".json",
            "transcript": "original history",
            "max_output_tokens": requested,
        },
    )
    assert result["status"] == "completed"
    if allowed is not None:
        assert calls[0]["max_output_tokens"] == allowed
    else:
        assert 32000 < calls[0]["max_output_tokens"] < 500000


def test_host_counts_dynamic_output_allowance_inside_summary_window(tmp_path):
    calls = []
    client = host(
        tmp_path, lambda **kwargs: calls.append(kwargs) or completed("Complete research state")
    )
    result = endpoint(client)(
        "POST",
        "/v1/summary",
        {
            "source": "context-history/snapshot-" + "b" * 64 + ".json",
            "transcript": "word " * 480000,
            "max_output_tokens": 32000,
        },
    )
    assert result["status"] == "completed"
    call = calls[0]
    assert call["max_output_tokens"] > 8192
    assert (
        estimate_request_tokens({k: v for k, v in call.items() if k != "model"})
        + call["max_output_tokens"]
        <= 500000
    )


def test_long_complete_summary_fits_total_target_and_preserves_fresh_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    local_config(monkeypatch)
    tools = [{"type": "function", "name": "read", "description": "bounded file read"}]
    extra = [{"role": "user", "content": "operational state " * 1000}]
    original = history()
    summary = "Verified result and original source. " * 2000
    assert len(summary.encode()) > 6000 and count_tokens(summary) > 4096
    calls = []

    def summarize(method, path, body, **kwargs):
        calls.append(body)
        assert body["max_output_tokens"] > 4096
        return completed(handoff_from_request(body, summary))

    candidate, record = cm.prepare(
        original, tools, summarize, extra_messages=extra, budget_limit=32000
    )
    total = (
        cm.message_cost(candidate)
        + cm.message_cost(extra)
        + cm.metric(tools)
        + 16 * len(tools)
        + 6000
    )
    assert total <= 32000
    assert record["after_estimated_tokens"] == total
    assert summary.strip() in candidate[0]["content"]
    assert candidate[-2:] == original[-2:]
    assert record and len(calls) == 1
    assert json.loads((state_paths.path(record["source"])).read_text()) == original + extra


def test_pure_summary_recovers_transient_error_without_changing_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    local_config(monkeypatch)
    original = history()
    source = cm.archive(original, "snapshot")
    calls = []
    now = [1000.0]
    monkeypatch.setattr(cm.time, "time", lambda: now[0])

    def summarize(*args, **kwargs):
        calls.append(args[-1])
        return (
            {"http_error": 500}
            if len(calls) == 1
            else completed(
                handoff_from_request(
                    args[-1], "Complete original observations and pending research actions."
                )
            )
        )

    with pytest.raises(cm.SummaryRetry) as pending:
        cm.full_summary(original, source, summarize, output_tokens=20000)
    now[0] += pending.value.retry_after
    text, _, _ = cm.full_summary(original, source, summarize, output_tokens=20000)
    assert "original observations" in text and len(calls) == 2
    assert json.loads((state_paths.path(source)).read_text()) == original
    assert (
        json.loads(calls[0]["transcript"])
        == json.loads(calls[1]["transcript"])
        == [{"source": index, "message": message} for index, message in enumerate(original, 1)]
    )


def test_saved_complete_summary_survives_a_smaller_summary_model_window(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    local_config(monkeypatch)
    original = history()
    source = cm.archive(original, "snapshot")
    text, _, _ = cm.full_summary(
        original,
        source,
        lambda *args, **kwargs: completed(
            handoff_from_request(
                args[-1], "Completed state with preserved source and next research action."
            )
        ),
        output_tokens=20000,
    )
    cm.TOKEN_CONFIG["summary_input_tokens"] = 100
    replay, _, _ = cm.full_summary(
        original,
        source,
        lambda *args, **kwargs: pytest.fail("cached summary must not be dispatched again"),
        output_tokens=20000,
    )
    assert replay == text


def test_failed_summary_publishes_a_source_quoted_degraded_handoff(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    local_config(monkeypatch)
    original = history()
    cm.atomic(state_paths.path("checkpoint.json"), json.dumps(original))
    calls = []
    fork = compaction.ForkCompactor(
        lambda *args, **kwargs: calls.append(args) or {"http_error": 422}
    )
    fork.start(original)
    fork.wait(5)
    candidate, record = fork.before_request(original)
    assert record["kind"] == "applied" and len(calls) == 1
    saved = json.loads((tmp_path / "conversation/compaction-result.json").read_text())
    assert saved["status"] == "ready"
    from a3_research_agent.harness.context import summary as research_summary

    assert research_summary.DEGRADED_NOTICE in candidate[0]["content"]
    assert "[source " in candidate[0]["content"]


def test_interrupted_fork_restarts_only_the_pure_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    local_config(monkeypatch)
    original = history()
    calls = []
    fork = compaction.ForkCompactor(
        lambda *args, **kwargs: (
            calls.append(args)
            or completed(
                handoff_from_request(
                    args[-1], "Recovered full research state with original sources and actions."
                )
            )
        )
    )
    cm.atomic(
        state_paths.path("compaction-result.json"),
        json.dumps(
            {
                "version": 2,
                "epoch": fork._epoch(),
                "fork_id": "interrupted",
                "status": "running",
                "applied": False,
                "prefix_length": len(original),
                "prefix_sha256": compaction.digest(original),
                "source": cm.archive(original, "snapshot"),
            }
        ),
    )
    same, _ = fork.before_request(original)
    fork.wait(5)
    candidate, record = fork.before_request(same)
    assert record["kind"] == "applied" and record["strategy"] == "host_summary"
    assert "Recovered full research state" in candidate[0]["content"]
    assert len(calls) == 1


def test_unpublished_legacy_fallback_requires_a_complete_new_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    local_config(monkeypatch)
    original = history()
    legacy = [{"role": "user", "content": "Earlier details not summarized; read original archive."}]
    fork = compaction.ForkCompactor(
        lambda *args, **kwargs: completed(
            handoff_from_request(
                args[-1], "Complete summary replaces the obsolete archive-only proposal."
            )
        )
    )
    cm.atomic(
        state_paths.path("compaction-result.json"),
        json.dumps(
            {
                "version": 2,
                "epoch": fork._epoch(),
                "fork_id": "legacy",
                "status": "fallback",
                "strategy": "archive_fallback",
                "applied": False,
                "prefix_length": len(original),
                "prefix_sha256": compaction.digest(original),
                "source": cm.archive(original, "snapshot"),
                "replacement": legacy,
                "replacement_sha256": compaction.digest(legacy),
            }
        ),
    )
    same, record = fork.before_request(original)
    assert same == original and record["kind"] == "fork_started"
    fork.wait(5)
    candidate, record = fork.before_request(original)
    assert record["kind"] == "applied" and record["strategy"] == "host_summary"
    assert "Complete summary replaces" in candidate[0]["content"]
    assert list(
        (tmp_path / "conversation/context-history").glob("rejected-compaction-result-*.json")
    )
