"""Explicit waits preserve notifications and deadlines without model polling."""

import json

import pytest

from a3_research_agent.harness import control_tools, operational_state, waiting


class Clock:
    def __init__(self):
        self.now = 0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_job_wait_targets_only_requested_jobs_and_renews_without_model_calls():
    clock, calls = Clock(), []
    a, b = "a" * 32, "b" * 32

    def request(method, path, body, **options):
        calls.append((method, path, body, options))
        clock.sleep(body["seconds"])
        return {
            "reason": "terminal" if len(calls) == 3 else "timeout",
            "jobs": [{"job_id": a, "status": "completed" if len(calls) == 3 else "running"}],
        }

    result = waiting.wait_for_jobs(request, [a], clock=clock.time, sleep=clock.sleep)
    assert result["reason"] == "state_changed"
    assert result["changes"]["/jobs/" + a]["status"] == "completed"
    assert all(call[2]["job_ids"] == [a] and b not in call[2]["job_ids"] for call in calls)
    assert clock.now == 75 and len(calls) == 3


def test_job_wait_timeout_is_total_duration_and_zero_polls_once():
    clock, holds = Clock(), []
    job = "a" * 32

    def request(method, path, body, **options):
        holds.append(body["seconds"])
        clock.sleep(body["seconds"])
        return {"reason": "timeout", "jobs": [{"job_id": job, "status": "running"}]}

    result = waiting.wait_for_jobs(
        request, [job], timeout_seconds=31, clock=clock.time, sleep=clock.sleep
    )
    assert holds == [25, 6] and clock.now == 31 and result["reason"] == "timeout"
    holds.clear()
    result = waiting.wait_for_jobs(
        request, [job], timeout_seconds=0, clock=clock.time, sleep=clock.sleep
    )
    assert holds == [0] and result["reason"] == "timeout"


def test_transient_failures_respect_deadline_and_permissions_return_immediately():
    clock = Clock()
    result = waiting.wait_for_jobs(
        lambda *a, **k: {"http_error": 502},
        ["a" * 32],
        timeout_seconds=5,
        clock=clock.time,
        sleep=clock.sleep,
    )
    assert result["reason"] == "timeout" and clock.now == 5
    result = waiting.wait_for_jobs(
        lambda *a, **k: {"http_error": 403}, ["a" * 32], clock=clock.time, sleep=clock.sleep
    )
    assert (
        result["reason"] == "observation_error" and result["http_status"] == 403 and clock.now == 5
    )


def test_idle_wait_never_observes_jobs_and_acknowledges_notification():
    clock, calls = Clock(), []

    def request(method, path, **options):
        calls.append((method, path))
        return {
            "schema": 1,
            "cursor": ("b" if clock.now >= 4 else "a") * 64,
            "pending": False,
            "feedback": {"revision": 2},
        }

    result = waiting.wait_idle(request, 30, clock=clock.time, sleep=clock.sleep, cursor="a" * 64)
    assert result["reason"] == "notification" and result["wake_cursor"] == "b" * 64
    assert clock.now == 4 and set(calls) == {("GET", "/wait/signals")}


def test_idle_wait_deadline_and_zero_do_not_requery_jobs():
    clock = Clock()

    def request(*args, **kwargs):
        return {"schema": 1, "cursor": "a" * 64, "pending": False}

    result = waiting.wait_idle(request, 5, clock=clock.time, sleep=clock.sleep)
    assert result["reason"] == "timeout" and clock.now == 5
    assert (
        waiting.wait_idle(lambda *a, **k: pytest.fail("zero idle wait contacted Host"), 0)["reason"]
        == "timeout"
    )


@pytest.mark.parametrize("mode", ["job", "idle"])
def test_input_revision_changes_interrupt_both_wait_modes(tmp_path, mode):
    clock = Clock()
    manifest = tmp_path / "input-manifest.json"
    manifest.write_text('{"status":"active","revision":1}')

    def sleep(seconds):
        clock.sleep(seconds)
        manifest.write_text('{"status":"active","revision":2}')

    def request(method, path, body=None, **options):
        if path == "/wait/signals":
            return {"schema": 1, "cursor": "a" * 64, "pending": False}
        return {"reason": "timeout", "jobs": []}

    kwargs = {"clock": clock.time, "sleep": sleep, "manifest_path": manifest}
    result = (
        waiting.wait_idle(request, 30, **kwargs)
        if mode == "idle"
        else waiting.wait_for_jobs(request, ["a" * 32], **kwargs)
    )
    assert result["reason"] == "inputs_changed"


def test_visible_wait_results_are_the_only_cursor_acknowledgement():
    cursor = "b" * 64
    for name in ("job_wait", "idle_wait"):
        call = {"type": "function_call", "name": name, "call_id": "one"}
        result = {
            "type": "function_call_output",
            "call_id": "one",
            "output": json.dumps({"ok": True, "wait_schema": 1, "wake_cursor": cursor}),
        }
        assert waiting.wait_cursor([call]) is None
        assert waiting.wait_cursor([call, result]) == cursor
        assert waiting.wait_cursor([result]) is None
    assert waiting.wait_cursor([{**call, "name": "wait"}, result]) is None


def test_control_wait_uses_observed_active_jobs_and_closes_terminal_receipts(monkeypatch):
    a, b = "a" * 32, "b" * 32
    for job in (a, b):
        operational_state.record_gateway("POST", "/jobs", {"job_id": job, "status": "queued"})

    def request(method, path, body, **kwargs):
        assert body["job_ids"] == [a, b]
        return {"reason": "terminal", "jobs": [{"job_id": a, "status": "completed"}]}

    result = control_tools.dispatch("job_wait", {}, request)
    assert result["reason"] == "state_changed" and waiting.active_jobs() == [b]
    assert control_tools.dispatch("job_wait", {"job_ids": []}, None)["reason"] == "no_jobs"


@pytest.mark.parametrize(
    "arguments",
    [
        {"job_ids": ["bad"]},
        {"timeout_seconds": True},
        {"timeout_seconds": -1},
        {"timeout_seconds": 601},
    ],
)
def test_bad_job_wait_arguments_return_helpful_errors(arguments):
    assert "error" in control_tools.dispatch(
        "job_wait", arguments, lambda *a, **k: pytest.fail("invalid request reached Host")
    )


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"http_error": 403},
        {"reason": "timeout", "jobs": "bad"},
        {"reason": "unknown", "jobs": []},
    ],
)
def test_all_job_observation_failures_have_the_same_error_contract(response):
    result = waiting.wait_for_jobs(lambda *a, **k: response, ["a" * 32])
    assert result["error"] == "wait_observation_failed" and result["reason"] == "observation_error"
    assert result["hint"]


def test_idle_service_error_has_a_failure_envelope_without_active_jobs():
    from a3_research_agent.harness.tool_results import normalize

    def fail(*args, **kwargs):
        raise OSError("connection unavailable")

    result = normalize(waiting.wait_idle(fail, 1))
    assert result["ok"] is False and result["error"] == "wait_observation_failed"
