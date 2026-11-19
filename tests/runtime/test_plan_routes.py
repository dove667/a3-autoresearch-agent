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


def test_plan_rejects_obsolete_goal_and_wrong_action_fields():
    assert (
        plan.dispatch({"action": "update", "revision": 0, "goal": "old"})["error"]
        == "plan_validation"
    )
    assert plan.dispatch({"action": "show", "steps": []})["error"] == "plan_validation"
    create()
    assert (
        plan.dispatch({"action": "update", "revision": 1, "direction": "wrong"})["error"]
        == "plan_validation"
    )


def test_web_research_gates_new_routes_and_changed_designs():
    create()
    route = {
        "id": "mixed-act",
        "question": "Does task conditioning help?",
        "hypothesis": "A shared policy benefits from task labels",
        "experiment": "Compare conditioned and unconditioned ACT",
        "decision_rule": "Use paired rollouts",
        "status": "active",
    }
    patch = {"action": "update", "revision": 1, "routes": [route]}
    assert plan.dispatch(patch, require_research=True)["error"] == "route_research_required"

    implementation = "https://github.com/huggingface/lerobot/blob/main/config.py"
    paper = "https://arxiv.org/html/2304.13705"
    research = {
        "implementation": [{"url": implementation, "finding": "ACT predicts action chunks"}],
        "papers": [{"url": paper, "finding": "Action chunking changes error accumulation"}],
        "design": "Hold chunk length fixed and ablate only task labels.",
    }
    route["research"] = research
    assert plan.dispatch(patch, require_research=True)["error"] == "unread_research_sources"
    operational_state.record_gateway(
        "POST",
        "/web/fetch",
        {"request_id": "source-code", "sources": [{"url": implementation, "text": "config"}]},
    )
    operational_state.record_gateway(
        "POST",
        "/web/fetch",
        {"request_id": "source-paper", "sources": [{"url": paper, "text": "methods"}]},
    )
    assert plan.dispatch(patch, require_research=True)["revision"] == 2
    assert plan.load()["routes"]["mixed-act"]["research"] == research

    patch = {
        "action": "update",
        "revision": 2,
        "reason": "Change research hypothesis",
        "routes": [{"id": "mixed-act", "hypothesis": "A separate action head is needed"}],
    }
    assert plan.dispatch(patch, require_research=True)["error"] == "route_research_required"
    assert plan.load()["revision"] == 2
