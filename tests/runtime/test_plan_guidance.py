"""Research guidance survives bounded context views and daily run changes."""

from a3_research_agent.harness import operational_state, paths, plan


def route(route_id="policy", **changes):
    return {
        "id": route_id,
        "question": "Which failure limits full evaluation?",
        "hypothesis": "Balanced observations improve placement",
        "experiment": "Compare a fixed candidate against baseline on paired seeds",
        "decision_rule": "Retain when placement improves without reducing task success",
        "status": "active",
        **changes,
    }


def step(step_id="evaluate", **changes):
    return {
        "id": step_id,
        "route": "policy",
        "task": "Evaluate candidate",
        "criterion": "Paired per-episode results are saved",
        **changes,
    }


def test_long_legal_research_guidance_keeps_the_only_ready_step_visible():
    text = "研究" * 600
    result = plan.dispatch(
        {
            "action": "update",
            "revision": 0,
            "direction": {
                "objective": text,
                "metrics": ["指标" * 299 + str(i) for i in range(20)],
                "gaps": ["缺口" * 299 + str(i) for i in range(20)],
            },
            "routes": [
                route(
                    question=text,
                    hypothesis=text,
                    experiment=text,
                    decision_rule=text,
                    conclusion=text,
                ),
                route("later", priority=100),
            ],
            "steps": [step()],
        }
    )
    assert result["focus"] == result["ready"] == ["evaluate"]
    assert list(result["steps"]) == ["evaluate"]
    assert list(result["routes"]) == ["policy"]
    assert result["omitted_routes"] == 1
    assert len(result["direction"]["objective"]) == 500
    assert len(result["direction"]["metrics"]) == 4
    assert len(result["routes"]["policy"]["hypothesis"]) == 300
    full = plan.dispatch({"action": "show"})
    assert full["direction"]["objective"] == text
    assert full["routes"]["policy"]["hypothesis"] == text
    assert len(full["direction"]["metrics"]) == 20


def test_review_pauses_route_across_runs_and_new_job_results_require_another_review(
    tmp_path, monkeypatch
):
    job = "a" * 32
    plan.dispatch(
        {
            "action": "update",
            "revision": 0,
            "direction": {"objective": "Improve full evaluation"},
            "routes": [route()],
            "steps": [step(jobs=[job])],
        }
    )
    operational_state.record_gateway("POST", "/jobs", {"job_id": job, "status": "completed"})
    assert plan.dispatch({"action": "show"})["review_due"] == [
        {"step": "evaluate", "route": "policy", "jobs": {job: "completed"}}
    ]
    result = plan.dispatch(
        {
            "action": "review",
            "revision": 1,
            "id": "policy",
            "decision": "pause",
            "conclusion": "Placement gains did not preserve task success",
            "evidence": [f"jobs/{job}/results.json"],
            "reason": "Investigate the regression before more training",
        }
    )
    assert result["ready"] == result["review_due"] == []
    original_snapshot_root = paths.plan_snapshots_root()
    snapshots = {p.name: p.read_bytes() for p in original_snapshot_root.glob("*.json")}
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path / "trajectories/day-two/conversation"))
    monkeypatch.setenv(
        "A3_PLAN_SNAPSHOTS_ROOT", str(tmp_path / "trajectories/day-two/plan-snapshots")
    )
    fresh = plan.dispatch({"action": "show"})
    assert fresh["revision"] == 2
    assert fresh["routes"]["policy"]["status"] == "paused"
    assert fresh["ready"] == fresh["review_due"] == []
    assert plan.load()["reviews"][-1]["observed_jobs"] == {job: "completed"}
    assert {p.name: p.read_bytes() for p in original_snapshot_root.glob("*.json")} == snapshots

    new_job = "b" * 32
    plan.dispatch(
        {"action": "update", "revision": 2, "steps": [{"id": "evaluate", "jobs": [new_job]}]}
    )
    operational_state.record_gateway("POST", "/jobs", {"job_id": new_job, "status": "failed"})
    changed = plan.dispatch({"action": "show"})
    assert changed["ready"] == []
    assert changed["review_due"] == [
        {"step": "evaluate", "route": "policy", "jobs": {new_job: "failed"}}
    ]


def test_focus_prioritizes_steps_then_route_and_excludes_paused_routes():
    result = plan.dispatch(
        {
            "action": "update",
            "revision": 0,
            "direction": {"objective": "Improve full evaluation"},
            "routes": [
                route(priority=10),
                route("alternative", priority=90),
                route("paused", status="paused"),
            ],
            "steps": [
                step("low", priority=1),
                step("primary", priority=20),
                step("alternative", route="alternative", priority=20),
                step("paused", route="paused", priority=100),
            ],
        }
    )
    assert result["ready"] == ["alternative", "primary", "low"]
    assert result["focus"] == ["alternative"]
    assert next(iter(result["steps"])) == "alternative"
