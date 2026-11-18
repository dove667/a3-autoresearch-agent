"""An unrepairable model handoff must not stop the run: sources rebuild it."""

import json

import pytest

from a3_research_agent.harness.context import manager as cm
from a3_research_agent.harness.context import summary as research_summary


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setattr(
        cm,
        "TOKEN_CONFIG",
        {
            "decision_input_tokens": 49000,
            "trigger_tokens": 40000,
            "target_tokens": 12000,
            "injected_reserve_tokens": 1000,
            "summary_context_tokens": 500000,
            "summary_input_tokens": 491808,
            "summary_reserve_tokens": 5000,
            "model_routes_sha256": "route-a",
        },
    )
    monkeypatch.setattr(cm.time, "sleep", lambda seconds: None)


def operational_view(objective, task, job, status):
    plan = {
        "direction": {"objective": objective, "metrics": [], "gaps": []},
        "focus": ["s1"],
        "ready": ["s1"],
        "steps": {"s1": {"task": task, "status": "active", "jobs": [job]}},
        "review_due": [{"step": "s1", "route": "r1", "jobs": {job: "failed"}}],
    }
    facts = {"active_jobs": {job: status}}
    return {
        "role": "user",
        "content": json.dumps(
            {"frame": "CURRENT_STATE_REFRESH", "research_plan": plan, "research_facts": facts},
            ensure_ascii=False,
        ),
    }


def history(job="a" * 32, status="running"):
    return [
        {"role": "user", "content": "启动研究并保存证据 " * 900},
        {"type": "function_call", "call_id": "c1", "name": "job_status", "arguments": "{}"},
        {
            "type": "function_call_output",
            "call_id": "c1",
            "output": json.dumps({"ok": True, "job_id": job, "status": status, "exit_code": 0}),
        },
        operational_view("提高 2×5 与 2×10 全量评测的入盒准确率", "补齐配对闭环评测", job, status),
    ]


def test_degraded_handoff_is_built_from_verifiable_sources(context):
    messages = history()
    document, rendered = research_summary.degraded(messages)

    report = research_summary.validate(document, messages)
    assert report["entries"] >= 3
    assert research_summary.DEGRADED_NOTICE in rendered
    assert any("提高 2×5" in item["quote"] for item in document["goal_corrections"])
    assert any("补齐配对闭环评测" in item["quote"] for item in document["next_actions"])
    assert document["active_jobs"] and "running" in document["active_jobs"][0]["quote"]
    for section in research_summary.SECTIONS:
        for item in document[section]:
            assert item["quote"] in research_summary.source_text(messages[item["source"] - 1])


def test_degraded_handoff_respects_the_summary_budget(context):
    messages = history()
    full, _ = research_summary.degraded(messages)
    document, rendered = research_summary.degraded(
        messages, fits=lambda value: cm.metric(value) <= 400
    )
    assert cm.metric(rendered) <= 400
    assert sum(len(document[section]) for section in research_summary.SECTIONS) < sum(
        len(full[section]) for section in research_summary.SECTIONS
    )
    research_summary.validate(document, messages)


def failing(reqs):
    def request(method, path, body=None, **kwargs):
        reqs.append((method, path))
        return {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "not json"}]}
            ],
        }

    return request


def test_unrepairable_summary_falls_back_instead_of_blocking(context):
    requests = []
    candidate, record = cm.prepare(history(), [], failing(requests), force_summary=True)
    assert record["summary_mode"] == "degraded"
    assert record["degraded_reason"]
    assert requests and all(path == "/v1/summary" for _, path in requests)
    assert research_summary.DEGRADED_NOTICE in candidate[0]["content"]
    assert cm.message_cost(candidate) <= cm.decision_budget([])
    assert cm.paired(candidate)


def test_repeated_failures_open_the_breaker_and_stop_model_calls(context):
    requests = []
    request = failing(requests)
    for round_index in range(2):
        messages = history(status="running") + [
            {"role": "user", "content": f"第 {round_index} 轮观察 " * 600}
        ]
        cm.prepare(messages, [], request, force_summary=True)
    called = len(requests)
    messages = history(status="completed") + [{"role": "user", "content": "后续观察 " * 600}]
    candidate, record = cm.prepare(messages, [], request, force_summary=True)
    assert len(requests) == called
    assert record["summary_mode"] == "degraded"
    assert record["degraded_reason"] == "summary_breaker_open"
    assert research_summary.DEGRADED_NOTICE in candidate[0]["content"]


def test_successful_summary_clears_the_breaker(context, monkeypatch):
    requests = []
    cm.record_summary_failure("previous failure")
    cm.record_summary_failure("previous failure")
    assert cm.summary_suspended()

    def good(method, path, body=None, **kwargs):
        requests.append(path)
        document = {"version": 1, **{section: [] for section in research_summary.SECTIONS}}
        source = json.loads(body["transcript"])[0]["message"]
        quote = research_summary.source_text(source)[:40]
        document["results"] = [{"text": quote, "source": 1, "quote": quote}]
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(document, ensure_ascii=False)}
                    ],
                }
            ],
        }

    monkeypatch.setenv(cm.state_paths.SUMMARY_BREAKER, "ignored")
    cm.state_paths.path(cm.state_paths.SUMMARY_BREAKER).unlink(missing_ok=True)
    _, record = cm.prepare(history(), [], good, force_summary=True)
    assert record["summary_mode"] == "model"
    assert not cm.summary_suspended()


def test_degraded_handoff_records_measured_results(context):
    """A finished job with a measurement belongs in Results, not queue traffic."""
    job = "b" * 32
    messages = [
        {"role": "user", "content": "开始评测 " * 800},
        {"type": "function_call", "call_id": "c9", "name": "job_status", "arguments": "{}"},
        {
            "type": "function_call_output",
            "call_id": "c9",
            "output": json.dumps(
                {
                    "ok": True,
                    "job_id": job,
                    "status": "completed",
                    "exit_code": 0,
                    "evaluation": {"cookies_in_target": 279, "episodes": 60},
                }
            ),
        },
    ]
    document, _ = research_summary.degraded(messages)
    assert document["results"]
    assert any("cookies_in_target" in item["quote"] for item in document["results"])
    research_summary.validate(document, messages)
