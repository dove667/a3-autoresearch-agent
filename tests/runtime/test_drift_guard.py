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


def test_a_clean_call_resets_the_incident_chain():
    guard = drift.DriftGuard()
    for _ in range(3):
        guard.observe([call("read", "{}")], invalid=True)
        guard.observe([call("list", '{"path":"x"}')], False)
    assert guard.observe([call("read", "{}")], invalid=True) == drift.INVALID_NOTICE


def test_invalid_calls_keep_getting_corrections_up_to_the_budget():
    guard = drift.DriftGuard()
    notices = [
        guard.observe([call("write", json.dumps({"path": "a.py", "n": index}))], invalid=True)
        for index in range(drift.MAX_INVALID)
    ]
    assert notices == [drift.INVALID_NOTICE] * drift.MAX_INVALID
    assert not guard.stalled()
    guard.observe([call("write", '{"path":"late.py"}')], invalid=True)
    assert guard.stalled()


def test_a_clean_call_clears_the_invalid_budget():
    guard = drift.DriftGuard()
    for index in range(drift.MAX_INVALID):
        guard.observe([call("write", json.dumps({"path": "b.py", "n": index}))], invalid=True)
    guard.observe([call("job_submit", '{"jobs":[]}')], False)
    assert guard.snapshot()["invalid"] == 0
    assert not guard.stalled()


def test_repeated_identical_calls_stop_after_the_repeat_budget():
    guard = drift.DriftGuard()
    same = [call("write", '{"path":"a.py"}')]
    guard.observe(same, False)
    for _ in range(drift.MAX_REPEAT):
        guard.observe(same, False)
    assert not guard.stalled()
    guard.observe(same, False)
    assert guard.stalled()


def test_a_tool_call_clears_the_empty_response_budget():
    guard = drift.DriftGuard()
    for _ in range(drift.MAX_NO_TOOL - 1):
        guard.observe_empty()
    assert not guard.stalled()
    guard.observe([call("read", "{}")], False)
    assert guard.snapshot()["no_tool"] == 0
    assert not guard.stalled()


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


def test_endless_reading_earns_a_reminder_but_keeps_every_tool():
    guard = drift.DriftGuard()
    notices = [guard.observe(read_turn(index), False) for index in range(drift.ANSWER_TURNS)]
    assert notices[:-1] == [None] * (drift.ANSWER_TURNS - 1)
    assert notices[-1] == drift.ANSWER_NOTICE
    assert not guard.stalled()
    assert guard.snapshot()["reading"] == 0


def test_the_reminder_repeats_without_ever_stopping_the_run():
    guard = drift.DriftGuard()
    notices = [guard.observe(read_turn(index), False) for index in range(drift.ANSWER_TURNS * 8)]
    assert notices.count(drift.ANSWER_NOTICE) == 8
    assert guard.snapshot()["answers"] == 8
    assert not guard.stalled()


def test_a_state_change_never_earns_the_reminder():
    guard = drift.DriftGuard()
    for index in range(drift.ANSWER_TURNS - 1):
        guard.observe(read_turn(index), False)
    assert guard.observe([{"name": "job_submit", "arguments": "{}"}], False) is None


def test_a_repeated_read_loop_still_gets_the_reminder():
    # An identical repeat is a different, older condition; the reminder must still appear.
    guard = drift.DriftGuard()
    same = {"name": "read", "arguments": json.dumps({"path": "f"})}
    notices = [guard.observe([same], False) for _ in range(drift.ANSWER_TURNS)]
    assert drift.ANSWER_NOTICE in notices
