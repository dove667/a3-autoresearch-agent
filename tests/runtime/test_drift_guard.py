"""A drifting model gets bounded corrections, not an open-ended prose loop."""

from a3_research_agent.harness import drift


def call(name, arguments="{}", call_id="c1"):
    return {"type": "function_call", "name": name, "arguments": arguments, "call_id": call_id}


def test_empty_response_forces_one_tool_call_and_is_bounded():
    guard = drift.DriftGuard()
    assert not guard.force_tool_choice()
    assert guard.observe_empty() == drift.NO_TOOL_NOTICE
    assert guard.force_tool_choice()
    guard.spent()
    guard.spent()
    assert not guard.force_tool_choice()
    assert guard.snapshot()["no_tool"] == 1


def test_repeated_identical_calls_get_a_repeat_notice():
    guard = drift.DriftGuard()
    same = [call("write", '{"path":"a.py"}')]
    assert guard.observe(same, False) is None
    assert guard.observe(same, False) == drift.REPEAT_NOTICE
    assert guard.force_tool_choice()


def test_polling_is_not_treated_as_a_repeat_loop():
    guard = drift.DriftGuard()
    for _ in range(4):
        assert guard.observe([call("job_status", '{"job_id":"a"}')], False) is None
    assert not guard.force_tool_choice()


def test_invalid_arguments_force_a_corrected_attempt():
    guard = drift.DriftGuard()
    assert guard.observe([call("job_submit", "{}")], invalid=True) == drift.INVALID_NOTICE
    assert guard.force_tool_choice()
    guard.spent()
    assert guard.observe([call("job_submit", "{}")], invalid=True) == drift.INVALID_NOTICE


def test_forced_corrections_are_capped_per_process():
    guard = drift.DriftGuard()
    notices = [guard.observe([call("read", "{}")], invalid=True) for _ in range(6)]
    assert notices[:3] == [drift.INVALID_NOTICE] * 3
    assert notices[3:] == [None] * 3


def test_a_clean_call_resets_the_incident_chain():
    guard = drift.DriftGuard()
    for _ in range(3):
        guard.observe([call("read", "{}")], invalid=True)
        guard.observe([call("list", '{"path":"x"}')], False)
    assert guard.observe([call("read", "{}")], invalid=True) == drift.INVALID_NOTICE
