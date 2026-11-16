"""Harness support for weaker models: measured decisions, diagnoses, digests."""

import json

import pytest

from a3_research_agent.harness import (
    job_failure,
    ledger,
    skill_registry,
    tool_catalog,
    tool_results,
)
from a3_research_agent.harness import plan as planning
from a3_research_agent.harness.context import manager as cm


def seed_plan():
    return planning.dispatch(
        {
            "action": "update",
            "revision": 0,
            "direction": {"objective": "提高 2×5 与 2×10 入盒准确率"},
            "routes": [
                {
                    "id": "r1",
                    "question": "扩采数据能否提升 2×10",
                    "hypothesis": "更多合格 DAgger 数据提高入盒数",
                    "experiment": "同种子配对评测",
                    "decision_rule": "配对结果改善则保留",
                }
            ],
            "steps": [
                {"id": "s1", "route": "r1", "task": "训练 ACT", "criterion": "完成 5000 步"},
            ],
            "reason": "start",
        }
    )


def test_review_requires_a_measured_value_to_continue():
    seed_plan()
    missing = planning.dispatch(
        {
            "action": "review",
            "revision": 1,
            "id": "r1",
            "decision": "continue",
            "conclusion": "数据有效",
            "evidence": ["experiments/e1/results/report.json"],
        }
    )
    assert missing["error"] == "plan_validation" and "metrics" in missing["hint"]
    unmeasured = planning.dispatch(
        {
            "action": "review",
            "revision": 1,
            "id": "r1",
            "decision": "continue",
            "conclusion": "数据有效",
            "evidence": ["experiments/e1/results/report.json"],
            "metrics": ["改善了"],
        }
    )
    assert unmeasured["error"] == "plan_validation"


def test_review_records_step_and_metrics():
    seed_plan()
    result = planning.dispatch(
        {
            "action": "review",
            "revision": 1,
            "id": "r1",
            "step": "s1",
            "decision": "continue",
            "conclusion": "2×10 改善",
            "evidence": ["experiments/e1/results/report.json"],
            "metrics": ["2x10 accuracy=0.42", "2x5 accuracy=0.50"],
        }
    )
    assert result["routes"]["r1"]["status"] == "active"
    stored = planning.load()
    review = stored["reviews"][0]
    assert review["step"] == "s1" and review["metrics"][0].startswith("2x10")


def test_review_step_must_belong_to_its_route():
    seed_plan()
    result = planning.dispatch(
        {
            "action": "review",
            "revision": 1,
            "id": "r1",
            "step": "missing",
            "decision": "pause",
            "conclusion": "暂停",
            "evidence": ["experiments/e1/results/report.json"],
        }
    )
    assert result["error"] == "plan_validation"


def test_job_failure_diagnosis_names_the_gap():
    result = {
        "status": "failed",
        "exit_code": 1,
        "output": "Traceback (most recent call last):\n  ...\nModuleNotFoundError: No module named 'lerobot'",
    }
    diagnosis = job_failure.diagnose(result)
    assert diagnosis["failure_class"] == "dependency_missing"
    assert "ModuleNotFoundError" in diagnosis["error_line"]
    assert diagnosis["suggested_action"]


def test_successful_job_has_no_diagnosis():
    assert job_failure.diagnose({"status": "completed", "exit_code": 0, "output": "ok"}) is None


def test_unknown_failure_stays_unclassified():
    diagnosis = job_failure.diagnose({"status": "failed", "exit_code": 3, "output": "boom"})
    assert diagnosis["failure_class"] == "unclassified"


def test_ledger_records_job_events_and_trims(tmp_path):
    line = ledger.from_tool(
        {"name": "job_submit", "call_id": "c1"},
        {"ok": True, "job_id": "a" * 32, "status": "queued"},
    )
    assert json.loads(line)["job_id"] == "a" * 32
    path = ledger.path()
    path.write_text("\n".join(json.dumps({"ts": i}) for i in range(ledger.KEEP_LINES + 50)) + "\n")
    ledger.trim(path)
    assert len(path.read_text().splitlines()) == ledger.KEEP_LINES


def test_ledger_skips_tools_without_observations():
    assert ledger.from_tool({"name": "read", "call_id": "c1"}, {"ok": True}) is None
    assert ledger.from_compaction({"summary_mode": "degraded", "degraded_reason": "x"})


def test_skill_digest_is_small_and_cached(monkeypatch):
    calls = []

    def dispatch(args):
        calls.append(args)
        return {
            "skills": [
                {"id": "a3-job-recovery", "description": "提交与恢复作业", "match_reason": "body"},
                {"id": "a3-training-progress", "description": "训练日志", "match_reason": "body"},
            ]
        }

    monkeypatch.setattr(skill_registry, "dispatch", dispatch)
    plan = {"direction": {"objective": "训练 ACT"}, "revision": 3, "steps": {"s": {"task": "预检"}}}
    first = skill_registry.suggestions(plan)
    second = skill_registry.suggestions(plan)
    assert first == second and len(calls) == 1
    assert [item["id"] for item in first["skills"]] == ["a3-job-recovery", "a3-training-progress"]
    assert all(len(item["description"]) <= 180 for item in first["skills"])


def test_unavailable_digest_does_not_raise(monkeypatch):
    monkeypatch.setattr(
        skill_registry, "dispatch", lambda args: {"error": "knowledge_service_unavailable"}
    )
    value = skill_registry.suggestions({"direction": {}, "revision": 1})
    assert value == {"skills": [], "notice": None}


def test_optional_tool_families_follow_deployment_capabilities():
    names = {entry["name"] for entry in tool_catalog.enabled({"web": False, "vision": True})}
    assert "web_search" not in names and "job_submit" in names
    assert len(tool_catalog.enabled({"web": True})) == len(tool_catalog.TOOLS)
    assert len(tool_catalog.enabled(None)) == len(tool_catalog.TOOLS)


@pytest.mark.parametrize("section", ["plan", "facts"])
def test_compact_views_stay_inside_their_budget(section):
    seed_plan()
    view = planning.view(planning.load(), jobs={}, compact=True)
    assert cm.size(view) <= 6000
    if section == "plan":
        assert view["overview"] and view["overview"][0]["id"] == "s1"
        assert view["parallelism"] == {"ready_steps": 1, "active_jobs": 0}


def test_controller_rejection_keeps_its_code_without_host_wording():
    missing = tool_results.normalize(
        {"http_error": 400, "body": json.dumps({"detail": "FileNotFoundError"})}
    )
    assert missing["error"] == "artifact_not_found" and "outputs" in missing["hint"]
    rejected = tool_results.normalize(
        {"http_error": 400, "body": json.dumps({"detail": "ValueError"})}
    )
    assert rejected["error"] == "artifact_request_rejected"
    coded = tool_results.normalize(
        {"http_error": 400, "body": json.dumps({"detail": {"code": "artifact_not_found"}})}
    )
    assert coded["error"] == "artifact_not_found"
    assert tool_results.normalize({"http_error": 400})["error"] == "host_request_failed"
