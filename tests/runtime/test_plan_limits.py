"""Long research notes must be kept, not rejected, and the step cap must fit batches."""

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


def test_a_batch_plan_can_hold_more_than_twenty_steps():
    assert model.MAX_STEPS >= 32
    steps = [{"id": f"s{index}", "task": "Batch", "criterion": "Measured"} for index in range(25)]
    revised = model.revise(plan_with({}), {"steps": steps})
    assert len(revised["steps"]) == 25
    assert model.ordered_steps(revised["steps"])[:3] == ["s0", "s1", "s2"]
