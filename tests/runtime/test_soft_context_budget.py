"""Soft context targets preserve fresh observations inside the real model budget."""

import json

import pytest

from a3_research_agent.harness import state_paths
from a3_research_agent.harness.context import manager as cm
from tests.runtime.summary_fixtures import handoff_from_request


def configure(monkeypatch, hard_budget):
    monkeypatch.setattr(cm, "metric", cm.size)
    monkeypatch.setattr(
        cm,
        "TOKEN_CONFIG",
        {
            "decision_input_tokens": hard_budget,
            "trigger_tokens": 16000,
            "target_tokens": 8000,
            "injected_reserve_tokens": 1000,
            "summary_input_tokens": 491808,
            "summary_reserve_tokens": 5000,
        },
    )


def provider(method, path, body, **kwargs):
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": handoff_from_request(body),
                    }
                ],
            }
        ],
    }


def observations(result):
    return [
        {"type": "function_call", "name": "job_status", "call_id": "fresh", "arguments": "{}"},
        cm.output_message(result, "fresh"),
    ]


def test_soft_target_expands_for_complete_fresh_batch(tmp_path, monkeypatch):
    configure(monkeypatch, 30000)
    fresh = observations({"status": "completed", "report": "R" * 10000})
    original = [{"role": "user", "content": "Historical observations. " * 3000}] + fresh
    candidate, record = cm.prepare(original, [], provider, force_summary=True)
    assert candidate[-2:] == fresh
    assert cm.paired(candidate) and cm.message_cost(candidate) <= cm.decision_budget([])
    assert record["soft_target_exceeded"] and not record["fresh_observations_fitted"]


def test_hard_budget_bounds_fresh_result_but_keeps_exact_fact_and_original(tmp_path, monkeypatch):
    configure(monkeypatch, 12000)
    result = {
        "job_id": "a" * 32,
        "status": "failed",
        "metrics": {"accuracy": 0.42},
        "report": "R" * 40000,
    }
    fresh = observations(result)
    original = [{"role": "user", "content": "Historical observations. " * 3000}] + fresh
    candidate, record = cm.prepare(original, [], provider, force_summary=True)
    assert cm.paired(candidate) and cm.message_cost(candidate) <= cm.decision_budget([])
    assert record["fresh_observations_fitted"]
    view = json.loads(candidate[-1]["output"])
    assert view["facts"]["metrics.accuracy"] == 0.42
    assert view["facts"]["job_id"] == "a" * 32
    assert view["facts"]["status"] == "failed"
    original_result = json.loads(state_paths.path(view["path"]).read_text())
    assert original_result == fresh[-1]


@pytest.mark.parametrize("hard_limit", [False, True])
def test_fit_counts_json_escaping_of_the_wrapped_summary(monkeypatch, hard_limit):
    tokenizer = cm.metric
    configure(monkeypatch, 30000)
    monkeypatch.setattr(cm, "metric", tokenizer)
    fresh = observations({"status": "completed", "report": "R" * 2000})
    original = [{"role": "user", "content": "Historical observations. " * 3000}] + fresh
    summary = ("Exact source: " + json.dumps('{"metric": 0.42, "status": "completed"}') + "\n") * 65
    source = cm.archive(original, "snapshot")
    estimated = cm.message_cost([cm.memory_message("", source)] + fresh) + cm.metric(summary)
    actual = cm.message_cost([cm.memory_message(summary, source)] + fresh)
    assert actual > estimated
    limit = estimated + 1000 + cm.metric([])
    monkeypatch.setitem(cm.TOKEN_CONFIG, "target_tokens", limit)
    if hard_limit:
        monkeypatch.setitem(cm.TOKEN_CONFIG, "decision_input_tokens", limit)

    def summarize(*args, output_tokens):
        assert cm.metric(summary) <= output_tokens
        return summary, {}, 0

    monkeypatch.setattr(cm, "full_summary", summarize)

    candidate, record = cm.prepare(original, [], provider, force_summary=True)

    assert candidate[-2:] == fresh
    assert cm.paired(candidate) and cm.message_cost(candidate) <= cm.decision_budget([])
    assert not record["fresh_observations_fitted"]
    if hard_limit:
        assert record["summary_mode"] == "degraded"
    else:
        assert candidate[0] == cm.memory_message(summary, source)
        assert record["effective_message_target_tokens"] == actual
        assert record["soft_target_exceeded"]
