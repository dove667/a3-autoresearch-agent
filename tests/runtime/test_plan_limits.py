"""Long research notes must be kept, not rejected, and the step cap must fit batches."""

import pytest

from a3_research_agent.harness.plan import model


def plan_with(steps):
    return {
        "revision": 0,
        "direction": {"objective": "Raise in-box accuracy", "metrics": [], "gaps": []},
        "steps": steps,
        "routes": {},
        "reviews": [],
        "reviewed_jobs": {},
    }


def test_long_progress_is_shortened_instead_of_rejected():
    step = model.merge_step(
        {}, {"id": "s1", "task": "Collect", "criterion": "40 episodes", "progress": "x" * 900}
    )
    assert len(step["progress"]) == 600
    assert step["progress"].endswith("…")


def test_short_progress_is_unchanged():
    step = model.merge_step(
        {}, {"id": "s1", "task": "Collect", "criterion": "40 episodes", "progress": "  done  "}
    )
    assert step["progress"] == "done"


def test_a_plan_can_hold_more_than_32_unfinished_steps():
    steps = [{"id": f"s{index}", "task": "Batch", "criterion": "Measured"} for index in range(70)]
    revised = model.revise(plan_with({}), {"steps": steps})
    assert len(revised["steps"]) == 70
    assert model.ordered_steps(revised["steps"])[:3] == ["s0", "s1", "s2"]


def test_closed_steps_do_not_use_execution_capacity():
    completed = {
        f"old{index}": model.merge_step({}, {"status": "done", "evidence": [f"report {index}"]})
        for index in range(32)
    }
    revised = model.revise(
        plan_with(completed), {"steps": [{"id": "next", "depends_on": ["old0"]}]}
    )
    assert revised["steps"]["old0"]["status"] == "done"
    assert revised["steps"]["next"]["depends_on"] == ["old0"]


def test_rewriting_a_completed_criterion_requires_new_evidence_or_reopening():
    step = model.merge_step(
        {}, {"status": "done", "criterion": "Dataset ready", "evidence": ["merge-report.json"]}
    )
    plan = plan_with({"s1": step})
    update = {"id": "s1", "criterion": "Mixed policy trained and evaluated"}
    with pytest.raises(ValueError, match="Reopen"):
        model.revise(plan, {"reason": "extend task", "steps": [update]})
    reopened = model.revise(
        plan, {"reason": "extend task", "steps": [{**update, "status": "pending"}]}
    )
    assert reopened["steps"]["s1"]["status"] == "pending"


def test_a_review_citing_a_beaten_baseline_is_flagged():
    from a3_research_agent.harness.plan import inconsistency

    plan = {
        "reviews": [
            {"route": "r1", "metrics": ["2x10 in-box=279/1200", "2x5 pass=4/60"]},
        ]
    }
    warnings = inconsistency.warnings(
        plan, "r1", "2×10 仍是 149/1200，需要继续", ["2x10 in-box=149/1200"]
    )
    assert any("149/1200" in warning and "279/1200" in warning for warning in warnings)


def test_a_stale_criterion_is_flagged_against_the_latest_measurement():
    from a3_research_agent.harness.plan import inconsistency

    plan = {"reviews": [{"route": "r1", "metrics": ["2x10 in-box=279/1200"]}]}
    warnings = inconsistency.warnings(
        plan, "r1", "继续", ["2x10 in-box=300/1200"], criterion="≥149/1200"
    )
    assert any("判据仍是 149/1200" in warning for warning in warnings)


def test_a_current_number_is_not_flagged():
    from a3_research_agent.harness.plan import inconsistency

    plan = {"reviews": [{"route": "r1", "metrics": ["2x10 in-box=279/1200"]}]}
    assert inconsistency.warnings(plan, "r1", "279/1200", ["2x10=280/1200"]) == []
