"""Research decisions and execution persist independently of model context."""

from a3_research_agent.harness import operational_state, plan


def create():
    return plan.dispatch(
        {
            "action": "update",
            "revision": 0,
            "direction": {
                "objective": "Improve full evaluation",
                "metrics": ["2x5 success", "2x10 accuracy"],
                "gaps": ["Placement stability"],
            },
            "routes": [
                {
                    "id": "stability",
                    "question": "Why unstable?",
                    "hypothesis": "Release too early",
                    "experiment": "Compare release timing",
                    "decision_rule": "Keep if paired success improves",
                    "status": "active",
                    "priority": 80,
                }
            ],
            "steps": [
                {
                    "id": "evaluate",
                    "route": "stability",
                    "task": "Evaluate timing",
                    "criterion": "Paired report",
                    "priority": 90,
                }
            ],
        }
    )


def test_results_require_explicit_review_and_paused_routes_stop_dispatch():
    assert create()["focus"] == ["evaluate"]
    job = "a" * 32
    plan.dispatch({"action": "update", "revision": 1, "steps": [{"id": "evaluate", "jobs": [job]}]})
    operational_state.record_gateway("POST", "/jobs", {"job_id": job, "status": "completed"})
    view = plan.dispatch({"action": "show"})
    assert view["review_due"][0]["route"] == "stability"
    assert (
        plan.dispatch(
            {
                "action": "review",
                "revision": 2,
                "id": "stability",
                "decision": "pause",
                "conclusion": "No gain",
            }
        )["error"]
        == "plan_validation"
    )
    result = plan.dispatch(
        {
            "action": "review",
            "revision": 2,
            "id": "stability",
            "decision": "pause",
            "conclusion": "No gain",
            "evidence": ["jobs/" + job],
        }
    )
    assert result["ready"] == [] and result["review_due"] == []
    assert plan.load()["reviews"][0]["observed_jobs"] == {job: "completed"}
    assert plan.dispatch({"action": "show"})["reviews"][0]["conclusion"] == "No gain"


def test_route_changes_need_reason_and_cannot_break_step_links(monkeypatch, tmp_path):
    create()
    assert (
        plan.dispatch(
            {
                "action": "update",
                "revision": 1,
                "routes": [{"id": "stability", "hypothesis": "Bad grip"}],
            }
        )["error"]
        == "plan_validation"
    )
    assert (
        plan.dispatch(
            {"action": "update", "revision": 1, "steps": [{"id": "evaluate", "route": "missing"}]}
        )["error"]
        == "plan_validation"
    )
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path / "next/conversation"))
    monkeypatch.setenv("A3_PLAN_SNAPSHOTS_ROOT", str(tmp_path / "next/plan-snapshots"))
    assert plan.dispatch({"action": "show"})["direction"]["gaps"] == ["Placement stability"]
    assert plan.load()["revision"] == 1
