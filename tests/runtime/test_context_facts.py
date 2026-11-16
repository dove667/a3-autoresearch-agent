"""Research receipts survive compaction and refresh from maintained sources."""

import base64
import json

import pytest
from summary_fixtures import handoff_from_request

from a3_research_agent.harness import state_paths
from a3_research_agent.harness.context import facts
from a3_research_agent.harness.context import manager as cm


def plan():
    return {"revision": 4, "reviews": [], "routes": {}}


def call(identity="a"):
    return {"name": "job_status", "call_id": identity}


def test_raw_receipt_survives_bounded_output_and_recovery_once(tmp_path, monkeypatch):
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path))
    result = {
        "job_id": "a" * 32,
        "status": "completed",
        "stdout": "noise" * 50000,
        "metrics": {"accuracy": 0.42},
    }
    facts.record(call(), result)
    bounded = cm.bound_tool_result(result, call())
    facts.record(call(), bounded)
    state = facts.load()
    assert len(state["receipts"]) == 1
    receipt = state["receipts"][0]
    assert receipt["facts"]["metrics.accuracy"] == 0.42
    original = json.loads(state_paths.path(receipt["source"]).read_text())
    assert json.loads(original["output"]) == result
    # The current source frame does not use or trust any generated summary text.
    refreshed = facts.view(plan(), {"a" * 32: {"status": "completed"}})
    assert refreshed["recent_receipts"][0]["facts"]["metrics.accuracy"] == 0.42
    assert not refreshed["active_jobs"]


def test_json_artifact_metrics_and_receipt_remain_together(tmp_path, monkeypatch):
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path))
    raw = json.dumps({"metrics": {"success_rate": 0.51}}).encode()
    facts.record(
        call(),
        {
            "encoding": "base64",
            "data": base64.b64encode(raw).decode(),
            "job_id": "b" * 32,
            "status": "completed",
        },
    )
    receipt = facts.load()["receipts"][0]
    assert receipt["facts"]["metrics.success_rate"] == 0.51
    assert receipt["facts"]["job_id"] == "b" * 32


def test_new_plan_decision_replaces_summary_pending_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path))
    current = plan()
    current["reviews"] = [
        {
            "route": "old",
            "decision": "pause",
            "conclusion": "Do not repeat this candidate",
            "evidence": ["jobs/test/report.json"],
        }
    ]
    current["routes"] = {
        "old": {"status": "paused", "conclusion": "Rejected " * 150, "evidence": []}
    }
    value = facts.view(current, {"b" * 32: {"status": "running"}})
    assert value["recent_reviews"][0]["decision"] == "pause"
    assert value["closed_routes"][0]["conclusion_complete"] is False
    assert value["active_jobs"] == {"b" * 32: "running"}
    current["reviews"][0]["decision"] = "continue"
    current["revision"] += 1
    assert facts.view(current, {})["recent_reviews"][0]["decision"] == "continue"


def test_receipt_index_is_bounded_and_reports_omissions(tmp_path, monkeypatch):
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path))
    for index in range(facts.MAX_RECEIPTS + 2):
        facts.record(call(str(index)), {"job_id": f"{index:032x}", "status": "completed"})
    state = facts.load()
    assert len(state["receipts"]) == facts.MAX_RECEIPTS and state["evicted"] == 2
    value = facts.view(plan(), {})
    assert value["omitted_receipts"] == facts.MAX_RECEIPTS + 2 - len(value["recent_receipts"])
    assert value["recent_receipts"][0]["call_id"] == str(facts.MAX_RECEIPTS + 1)


def test_receipt_corruption_or_reused_call_identity_stops_publication(tmp_path, monkeypatch):
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path))
    facts.record(call(), {"job_id": "a" * 32, "status": "running"})
    with pytest.raises(cm.ContextError, match="identity changed"):
        facts.record(call(), {"job_id": "a" * 32, "status": "completed"})
    state_paths.path(facts.INDEX).write_text('{"version":1,"receipts":[{}],"evicted":0}')
    with pytest.raises(cm.ContextError, match="Invalid research receipt"):
        facts.view(plan(), {})


def test_repeated_compaction_refreshes_exact_metrics_and_closed_work(tmp_path, monkeypatch):
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        cm,
        "TOKEN_CONFIG",
        {
            "decision_input_tokens": 49000,
            "trigger_tokens": 40000,
            "target_tokens": 16000,
            "injected_reserve_tokens": 1000,
            "summary_input_tokens": 491808,
            "summary_reserve_tokens": 5000,
        },
    )
    result = {
        "job_id": "a" * 32,
        "status": "completed",
        "stdout": "noise" * 15000,
        "metrics": {"accuracy": 0.42, "success_rate": 0.21},
    }
    facts.record(call(), result)
    current = plan()
    current["routes"] = {
        "candidate": {
            "status": "paused",
            "conclusion": "Candidate rejected; reuse audited results",
            "evidence": ["jobs/a/report.json"],
        }
    }
    state = [{"role": "user", "content": "Research work; preserve the evidence."}]
    requests = []

    def provider(method, path, body):
        requests.append(body)
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

    for round_index in range(2):
        state += [{"role": "user", "content": "Long exploration details. " * 7000}]
        refresh = {
            "role": "user",
            "content": json.dumps(
                {
                    "frame": "CURRENT_STATE_REFRESH",
                    "research_facts": facts.view(current, {}),
                }
            ),
        }
        state, record = cm.prepare(
            state, [], provider, extra_messages=[refresh], force_summary=True
        )
        assert cm.paired(state)
        transcript = json.loads(requests[-1]["transcript"])
        assert transcript[-1] == refresh
        assert json.loads(state_paths.path(record["source"]).read_text())[-1] == refresh
        restored = json.loads(refresh["content"])["research_facts"]
        assert restored["recent_receipts"][0]["facts"]["metrics.accuracy"] == 0.42
        assert restored["closed_routes"][0]["status"] == "paused"
        assert restored["plan_revision"] == current["revision"]
        current["revision"] += 1
    assert len(requests) == 2
