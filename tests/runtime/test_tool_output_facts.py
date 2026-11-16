"""Keep actionable research facts visible when surrounding output is archived."""

import base64
import json

import pytest

from a3_research_agent.harness import state_paths
from a3_research_agent.harness.context import manager as cm
from a3_research_agent.harness.context.tool_output import select_facts


def test_large_middle_metrics_and_receipt_survive_preview(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = {
        "logs": "noise " * 30000,
        "metrics": {"accuracy": 0.875, "native_success": 0.62},
        "error": "evaluation_failed",
        "job_id": "a" * 32,
        "ok": False,
        "status": "failed",
        "hint": "Inspect the evaluation manifest.",
        "tail": "noise " * 30000,
    }
    bounded = cm.bound_tool_result(result, {"call_id": "middle"})
    assert bounded["facts"]["metrics.accuracy"] == 0.875
    assert bounded["facts"]["metrics.native_success"] == 0.62
    assert bounded["facts"]["job_id"] == "a" * 32
    assert bounded["facts"]["error"] == "evaluation_failed"
    assert bounded["facts"]["ok"] is False
    assert cm.size(cm.output_message(bounded, "middle")) <= 12000
    original = json.loads(state_paths.path(bounded["path"]).read_text())
    assert json.loads(original["output"]) == result


def test_fact_keys_late_in_large_mapping_have_priority():
    value = {f"noise_{i}": i for i in range(3000)}
    value.update(metrics={"accuracy": 0.99}, job_id="b" * 32)
    facts, _, complete = select_facts(value)
    assert facts["job_id"] == "b" * 32
    assert facts["metrics.accuracy"] == 0.99
    assert not complete


def test_typed_json_artifact_exposes_metrics_without_base64(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    body = json.dumps({"logs": "x" * 40000, "accuracy": 0.7})
    result = {"encoding": "base64", "data": base64.b64encode(body.encode()).decode()}
    bounded = cm.bound_tool_result(result, {"call_id": "artifact"})
    assert bounded["facts"]["accuracy"] == 0.7
    assert bounded["artifact"]["encoding"] == "utf-8"
    assert state_paths.path(bounded["artifact"]["path"]).read_text() == body


def test_fact_selection_skips_opaque_and_private_fields():
    facts, _, _ = select_facts(
        {
            "metrics": {"api_key": "private", "access_token": "private", "accuracy": 0.8},
            "content": {"accuracy": "private"},
            "data": {"status": "private"},
        }
    )
    assert facts == {"metrics.accuracy": 0.8}


def test_fact_selection_has_bounded_depth_and_count():
    value = {"metrics": {f"score_{i}": i for i in range(2000)}}
    facts, omitted, complete = select_facts(value)
    assert len(facts) <= 48
    assert omitted and not complete


def test_tight_budget_preserves_primary_ids_or_fails_explicitly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = {
        "job_id": "a" * 32,
        "status": "failed",
        "error": "dataset_missing",
        "log": "x" * 20000,
    }
    original = cm.output_message(result, "budget")
    bounded = cm.archived_output(original, 1000)
    assert cm.size(bounded) <= 1000
    assert json.loads(bounded["output"])["facts"]["job_id"] == "a" * 32
    with pytest.raises(cm.ContextError, match="metadata"):
        cm.archived_output(original, 150)


def test_long_error_and_hint_have_explicit_partial_fields():
    error, hint = "missing dataset " * 100, "read manifest " * 100
    facts, omitted, _ = select_facts({"error": error, "hint": hint, "job_id": "x" * 32})
    assert facts["job_id"] == "x" * 32
    assert facts["error_preview"] == error[:200]
    assert facts["error_characters"] == len(error)
    assert facts["hint_preview"] == hint[:200]
    assert "error" not in facts and "hint" not in facts
    assert omitted == 2
