"""A drifting model gets bounded corrections, not an open-ended prose loop."""

import json

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
    assert drift.INVALID_NOTICE not in notices[3:]
    assert guard.snapshot()["incidents"] > drift.MAX_INCIDENTS


def test_a_clean_call_resets_the_incident_chain():
    guard = drift.DriftGuard()
    for _ in range(3):
        guard.observe([call("read", "{}")], invalid=True)
        guard.observe([call("list", '{"path":"x"}')], False)
    assert guard.observe([call("read", "{}")], invalid=True) == drift.INVALID_NOTICE


def test_read_only_turns_are_counted_and_a_state_change_clears_them():
    guard = drift.DriftGuard()
    for index in range(drift.ANSWER_TURNS - 1):
        guard.observe(read_turn(index), False)
    assert guard.snapshot()["reading"] == drift.ANSWER_TURNS - 1
    guard.observe([{"name": "job_submit", "arguments": "{}"}], False)
    assert guard.snapshot()["reading"] == 0


def test_plan_and_memory_reads_count_as_reading_but_a_review_does_not():
    assert drift.read_only({"name": "plan", "arguments": '{"action":"show"}'})
    assert drift.read_only({"name": "memory", "arguments": '{"action":"search"}'})
    assert drift.read_only({"name": "list", "arguments": "{}"})
    assert not drift.read_only({"name": "plan", "arguments": '{"action":"review"}'})
    assert not drift.read_only({"name": "memory", "arguments": '{"action":"write"}'})
    assert not drift.read_only({"name": "plan", "arguments": "not json"})
    assert not drift.read_only({"name": "job_submit", "arguments": "{}"})


def read_turn(index):
    return [{"name": "read", "arguments": json.dumps({"path": f"f{index}"})}]


def test_endless_reading_ends_with_a_demanded_answer():
    guard = drift.DriftGuard()
    notices = [guard.observe(read_turn(index), False) for index in range(drift.ANSWER_TURNS)]
    assert notices[:-1] == [None] * (drift.ANSWER_TURNS - 1)
    assert notices[-1] == drift.ANSWER_NOTICE
    assert guard.wants_answer()
    assert guard.snapshot()["answers"] == 1
    assert guard.snapshot()["reading"] == 0


def test_an_answer_resumes_tool_use_and_resets_the_reading_count():
    guard = drift.DriftGuard()
    for index in range(drift.ANSWER_TURNS):
        guard.observe(read_turn(index), False)
    guard.answered()
    assert not guard.wants_answer()
    assert guard.snapshot()["reading"] == 0


def test_a_state_change_never_asks_for_an_answer():
    guard = drift.DriftGuard()
    for index in range(drift.ANSWER_TURNS - 1):
        guard.observe(read_turn(index), False)
    assert guard.observe([{"name": "job_submit", "arguments": "{}"}], False) is None
    assert not guard.wants_answer()


def test_only_a_few_answers_are_demanded_before_the_process_ends():
    guard = drift.DriftGuard()
    for index in range(drift.ANSWER_TURNS * 5):
        guard.observe(read_turn(index), False)
        if guard.wants_answer():
            guard.answered()
    assert guard.snapshot()["answers"] == drift.MAX_ANSWERS
    assert not guard.wants_answer()
    assert guard.stalled()


def test_a_repeated_read_loop_still_ends_in_a_demanded_answer():
    guard = drift.DriftGuard()
    same = {"name": "read", "arguments": json.dumps({"path": "f"})}
    notices = [guard.observe([same], False) for _ in range(drift.ANSWER_TURNS)]
    assert drift.ANSWER_NOTICE in notices
    assert guard.wants_answer()
