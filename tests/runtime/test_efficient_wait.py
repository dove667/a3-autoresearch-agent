"""No real sleeps, provider calls, jobs or live run state."""

import json

import pytest

from a3_research_agent.harness import waiting


def load(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return waiting


class Clock:
    def __init__(self):
        self.now = 0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def setup_wait(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    (tmp_path / "operational-state.json").write_text(
        json.dumps({"jobs": {"job-1": {"status": "running"}}})
    )
    clock = Clock()
    return m, clock


def test_unchanged_job_waits_requested_deadline_without_model_roundtrips(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    calls = []

    def req(method, path, data=None, **options):
        calls.append(path)
        return {"status": "running"}

    result = m.wait_for_change(req, 120, clock=clock.time, sleep=clock.sleep)
    assert clock.now == 120
    assert result["reason"] == "deadline"
    assert 2 <= len(calls) <= 14


def test_job_completion_returns_before_deadline(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    result = m.wait_for_change(
        lambda *a, **k: {"status": "exited" if clock.now >= 20 else "running", "exit_code": 7},
        120,
        clock=clock.time,
        sleep=clock.sleep,
    )
    assert clock.now == 20
    assert result["reason"] == "state_changed"
    assert result["changes"]["/jobs/job-1"]["exit_code"] == 7


def test_request_error_is_reported_without_automatic_replay(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    result = m.wait_for_change(
        lambda *a, **k: {"http_error": 403}, 120, clock=clock.time, sleep=clock.sleep
    )
    assert result["reason"] == "observation_error"
    assert result["http_error"] == 403
    assert clock.now == 0


def test_input_revision_change_interrupts_wait(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"revision": "one", "status": "active"}))

    def sleep(seconds):
        clock.sleep(seconds)
        manifest.write_text(json.dumps({"revision": "two", "status": "active"}))

    result = m.wait_for_change(
        lambda *a, **k: {"status": "running"},
        120,
        clock=clock.time,
        sleep=sleep,
        manifest_path=manifest,
    )
    assert result["reason"] == "inputs_changed"
    assert clock.now == 10


@pytest.mark.parametrize("seconds", [-1, True, 601, 1.5])
def test_invalid_deadline_fails_explicitly(tmp_path, monkeypatch, seconds):
    m, clock = setup_wait(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        m.wait_for_change(None, seconds, clock=clock.time, sleep=clock.sleep)


def test_known_job_failure_interrupts_wait(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    (tmp_path / "operational-state.json").write_text(
        json.dumps({"jobs": {"job-1": {"status": "running"}}})
    )

    def req(method, path, **options):
        return (
            {"status": "failed", "exit_code": 9} if path == "/jobs/job-1" else {"status": "running"}
        )

    result = m.wait_for_change(req, 600, clock=clock.time, sleep=clock.sleep)
    assert clock.now == 0
    assert result["changes"]["/jobs/job-1"]["status"] == "failed"


def test_restart_observes_already_finished_job_without_job_replay(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    calls = []

    def req(method, path, **options):
        calls.append((method, path))
        return {"status": "exited", "exit_code": 0}

    result = m.wait_for_change(req, 600, clock=clock.time, sleep=clock.sleep)
    assert result["reason"] == "state_changed"
    assert calls == [("GET", "/jobs/job-1")]
    assert clock.now == 0


def test_malformed_status_is_not_silently_treated_as_waiting(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    result = m.wait_for_change(lambda *a, **k: {}, 600, clock=clock.time, sleep=clock.sleep)
    assert result["reason"] == "observation_error"
    assert clock.now == 0


def test_deadline_is_checked_between_slow_status_requests(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    (tmp_path / "operational-state.json").write_text(
        json.dumps({"jobs": {"job-1": {"status": "running"}, "job-2": {"status": "running"}}})
    )
    calls = []

    def req(method, path, **options):
        calls.append(path)
        clock.now += options["timeout"]
        return {"status": "running"}

    result = m.wait_for_change(req, 5, clock=clock.time, sleep=clock.sleep)
    assert result["reason"] == "deadline"
    assert clock.now == 5
    assert len(calls) == 1


def test_initial_updating_inputs_do_not_enter_sleep(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"status": "updating"}))
    result = m.wait_for_change(
        lambda *a, **k: {"status": "running"},
        600,
        clock=clock.time,
        sleep=clock.sleep,
        manifest_path=manifest,
    )
    assert result["reason"] == "inputs_changed"
    assert clock.now == 0


def test_deadline_result_retains_last_observed_job_state(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    result = m.wait_for_change(
        lambda *a, **k: {"status": "running"}, 20, clock=clock.time, sleep=clock.sleep
    )
    assert result["observed"]["/jobs/job-1"]["status"] == "running"


def signal(cursor="a" * 64, pending=False):
    return {"schema": 1, "cursor": cursor, "pending": pending, "feedback": {"count": 1}}


def test_new_feedback_wakes_within_signal_poll_not_wait_deadline(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    result = m.wait_for_change(
        lambda *a, **k: {"status": "running"},
        600,
        clock=clock.time,
        sleep=clock.sleep,
        signals=lambda timeout: signal("b" * 64 if clock.now >= 2 else "a" * 64),
    )
    assert clock.now == 2
    assert result["reason"] == "notification"
    assert result["wake_cursor"] == "b" * 64


def test_unchanged_notifications_do_not_return_to_model(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    checks = []

    def observe(timeout):
        checks.append(clock.now)
        return signal()

    result = m.wait_for_change(
        lambda *a, **k: {"status": "running"},
        20,
        clock=clock.time,
        sleep=clock.sleep,
        signals=observe,
        cursor="a" * 64,
    )
    assert result["reason"] == "deadline"
    assert clock.now == 20
    assert len(checks) <= 11


def test_pending_feedback_without_previous_cursor_wakes_immediately(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    result = m.wait_for_change(
        lambda *a, **k: {"status": "running"},
        600,
        clock=clock.time,
        sleep=clock.sleep,
        signals=lambda timeout: signal(pending=True),
    )
    assert result["reason"] == "notification"
    assert clock.now == 0


def test_notification_reads_do_not_acknowledge_crash_before_checkpoint(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    args = {"clock": clock.time, "sleep": clock.sleep, "signals": lambda timeout: signal("b" * 64)}
    for _ in range(2):
        result = m.wait_for_change(
            lambda *a, **k: {"status": "running"}, 600, cursor="a" * 64, **args
        )
        assert result["reason"] == "notification"
        assert clock.now == 0
    messages = [
        {"type": "function_call", "name": "wait", "call_id": "c"},
        {"type": "function_call_output", "call_id": "c", "output": json.dumps(result)},
    ]
    assert m.wait_cursor(messages) == "b" * 64
    assert m.wait_cursor(messages[:1]) is None
    after = m.wait_for_change(
        lambda *a, **k: {"status": "running"}, 4, cursor=m.wait_cursor(messages), **args
    )
    assert after["reason"] == "deadline"
    assert clock.now == 4


def test_signal_error_is_visible_and_not_slept_through(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    result = m.wait_for_change(
        lambda *a, **k: {"status": "running"},
        600,
        clock=clock.time,
        sleep=clock.sleep,
        signals=lambda timeout: {"http_error": 503},
    )
    assert result["reason"] == "observation_error"
    assert result["path"] == "/wait/signals"
    assert clock.now == 0


def test_slow_status_request_does_not_starve_notification_check(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    (tmp_path / "operational-state.json").write_text(
        json.dumps({"jobs": {"job-" + str(i): {"status": "running"} for i in range(3)}})
    )

    def status(*a, **k):
        clock.now += k["timeout"]
        return {"status": "running"}

    result = m.wait_for_change(
        status,
        600,
        clock=clock.time,
        sleep=clock.sleep,
        signals=lambda timeout: signal("b" * 64 if clock.now >= 2 else "a" * 64),
    )
    assert result["reason"] == "notification"
    assert clock.now == 2


def test_event_on_deadline_remains_visible_on_next_wait(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    observe = lambda timeout: signal("b" * 64 if clock.now >= 2 else "a" * 64)
    result = m.wait_for_change(
        lambda *a, **k: {"status": "running"},
        2,
        clock=clock.time,
        sleep=clock.sleep,
        signals=observe,
    )
    assert result["reason"] == "deadline"
    assert result["wake_cursor"] == "a" * 64
    following = m.wait_for_change(
        lambda *a, **k: {"status": "running"},
        2,
        clock=clock.time,
        sleep=clock.sleep,
        signals=observe,
        cursor=result["wake_cursor"],
    )
    assert following["reason"] == "notification"
    assert clock.now == 2


def test_raw_result_journal_restores_notification_cursor_before_next_wait(tmp_path, monkeypatch):
    from test_harness_context_recovery import agent_functions, initial_state, load_context

    m, _clock = setup_wait(tmp_path, monkeypatch)
    context = load_context()
    functions = agent_functions(context)
    old = initial_state(tmp_path)
    notice = {"wait_schema": 1, "reason": "notification", "wake_cursor": "b" * 64}
    call = {"type": "function_call", "name": "wait", "call_id": "call-wait", "arguments": "{}"}
    (tmp_path / "pending-calls.json").write_text(
        json.dumps(
            {
                "base_hash": functions["checkpoint_hash"](),
                "output": [call],
                "results": {"call-wait": notice},
            }
        )
    )
    restored = functions["recover_journal"](old)
    assert m.wait_cursor(restored) == "b" * 64
    assert json.loads((tmp_path / "pending-calls.json").read_text()) is None
    assert context.paired(restored)


def test_unrelated_or_uncommitted_messages_cannot_acknowledge_wait_signal(tmp_path, monkeypatch):
    m, _clock = setup_wait(tmp_path, monkeypatch)
    output = {
        "type": "function_call_output",
        "call_id": "read",
        "output": json.dumps({"wait_schema": 1, "wake_cursor": "b" * 64}),
    }
    assert (
        m.wait_cursor([{"type": "function_call", "name": "read", "call_id": "read"}, output])
        is None
    )
    assert m.wait_cursor([output]) is None


def test_overrun_request_returns_deadline_without_negative_sleep(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)

    def status(*a, **k):
        clock.now += k["timeout"] + 0.1
        return {"status": "running"}

    def sleep(seconds):
        assert seconds >= 0
        clock.sleep(seconds)

    assert m.wait_for_change(status, 5, clock=clock.time, sleep=sleep)["reason"] == "deadline"


def test_two_waits_in_one_model_batch_keep_prebatch_cursor(tmp_path, monkeypatch):
    import ast
    from types import SimpleNamespace

    from a3_research_agent.harness import planning

    m, clock = setup_wait(tmp_path, monkeypatch)
    from pathlib import Path

    ROOT = Path(__file__).resolve().parents[2] / "src/a3_research_agent/harness"

    source = (ROOT / "agent.py").read_text()
    function = next(
        x for x in ast.parse(source).body if isinstance(x, ast.FunctionDef) and x.name == "tool"
    )
    namespace = {
        "web_tools": None,
        "skill_tools": None,
        "WAIT_CURSOR": "a" * 64,
        "planning": planning,
        "messages": [
            {"type": "function_call", "name": "wait", "call_id": "one"},
            {"type": "function_call", "name": "wait", "call_id": "two"},
        ],
        "req": lambda *a, **k: signal("b" * 64),
        "waiting": SimpleNamespace(
            wait_cursor=m.wait_cursor,
            active_jobs=list,
            wait_for_change=lambda req, seconds, **kw: m.wait_for_change(
                lambda *a, **k: {"status": "running"},
                seconds,
                clock=clock.time,
                sleep=clock.sleep,
                **kw,
            ),
        ),
    }
    exec(  # noqa: S102 - trusted repository function only, not the Agent entry loop
        compile(ast.Module(body=[function], type_ignores=[]), "wait-dispatch", "exec"), namespace
    )
    first = namespace["tool"]("wait", {"seconds": 600})
    assert first["reason"] == "notification"
    namespace["messages"].append(
        {"type": "function_call_output", "call_id": "one", "output": json.dumps(first)}
    )
    second = namespace["tool"]("wait", {"seconds": 600})
    assert second["reason"] == "notification"
    assert clock.now == 0


def test_managed_wait_stays_inside_harness_across_timeouts_and_retries(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    results = iter(
        [
            {"http_error": 502},
            {"reason": "timeout", "jobs": [{"job_id": "job-1", "status": "queued"}]},
            {"reason": "timeout", "jobs": [{"job_id": "job-1", "status": "running"}]},
            {
                "reason": "terminal",
                "jobs": [{"job_id": "job-1", "status": "completed", "exit_code": 0}],
            },
        ]
    )
    calls = []

    def req(method, path, body, **kwargs):
        calls.append((method, path, body))
        clock.sleep(body["seconds"])
        return next(results)

    result = m.wait_for_jobs(req, ["job-1"], cursor="a" * 64, clock=clock.time, sleep=clock.sleep)
    assert result["reason"] == "state_changed"
    assert result["changes"] == {"/jobs/job-1": {"status": "completed", "exit_code": 0}}
    assert len(calls) == 4
    assert all(method == "POST" and path == "/jobs/wait" for method, path, _ in calls)


def test_wait_receipt_closes_only_terminal_targets(tmp_path, monkeypatch):
    from a3_research_agent.harness.memory import state

    monkeypatch.chdir(tmp_path)
    a, b = "a" * 32, "b" * 32
    for job in (a, b):
        state.record_gateway("POST", "/jobs", {"job_id": job, "status": "queued"})
    result = {
        "reason": "state_changed",
        "changes": {f"/jobs/{a}": {"status": "completed"}, f"/jobs/{b}": {"status": "running"}},
    }
    state.record_wait(result)
    revision = state.load()["revision"]
    state.record_wait(result)
    assert waiting.active_jobs() == [b]
    assert state.load()["revision"] == revision


def test_managed_wait_observes_jobs_beyond_first_batch():
    clock = Clock()
    jobs = [f"job-{i}" for i in range(65)]
    batches = []

    def req(method, path, body, **kwargs):
        batches.append(body["job_ids"])
        if len(batches) == 1:
            clock.sleep(body["seconds"])
            return {"reason": "timeout", "jobs": []}
        return {"reason": "terminal", "jobs": [{"job_id": jobs[-1], "status": "failed"}]}

    result = waiting.wait_for_jobs(req, jobs, clock=clock.time, sleep=clock.sleep)
    assert batches == [jobs[:64], jobs[64:]]
    assert result["changes"] == {"/jobs/job-64": {"status": "failed"}}
