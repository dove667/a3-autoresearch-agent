"""No real sleeps, provider calls, jobs or live run state."""

import json

import pytest
from test_work_assignments import load


class Clock:
    def __init__(self):
        self.now = 0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def setup_wait(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    m.save({"version": 1, "assignments": {"a": {"status": "assigned", "session_id": "worker-1"}}})
    clock = Clock()
    return m, clock


def test_unchanged_worker_waits_requested_deadline_without_model_roundtrips(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    calls = []

    def req(method, path, data=None, **options):
        calls.append(path)
        return {"status": "running"}

    result = m.wait_for_change(req, 120, clock=clock.time, sleep=clock.sleep)
    assert clock.now == 120
    assert result["reason"] == "deadline"
    assert 2 <= len(calls) <= 14


def test_worker_completion_returns_before_deadline(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    result = m.wait_for_change(
        lambda *a, **k: {"status": "exited" if clock.now >= 20 else "running", "exit_code": 7},
        120,
        clock=clock.time,
        sleep=clock.sleep,
    )
    assert clock.now == 20
    assert result["reason"] == "state_changed"
    assert result["changes"]["/sessions/worker-1"]["exit_code"] == 7


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


def test_already_pending_decision_returns_without_wait_or_network(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    m.save(
        {
            "version": 1,
            "assignments": {"a": {"status": "awaiting_decision", "session_id": "worker-1"}},
        }
    )

    def req(*a, **k):
        pytest.fail("No status request needed for an already delivered result")

    assert (
        m.wait_for_change(req, 600, clock=clock.time, sleep=clock.sleep)["reason"]
        == "decision_pending"
    )


def test_restart_observes_already_finished_worker_without_job_replay(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    calls = []

    def req(method, path, **options):
        calls.append((method, path))
        return {"status": "exited", "exit_code": 0}

    result = m.wait_for_change(req, 600, clock=clock.time, sleep=clock.sleep)
    assert result["reason"] == "state_changed"
    assert calls == [("GET", "/sessions/worker-1")]
    assert clock.now == 0


def test_malformed_status_is_not_silently_treated_as_waiting(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    result = m.wait_for_change(lambda *a, **k: {}, 600, clock=clock.time, sleep=clock.sleep)
    assert result["reason"] == "observation_error"
    assert clock.now == 0


def test_deadline_is_checked_between_slow_status_requests(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    m.save(
        {
            "version": 1,
            "assignments": {
                "a": {"status": "assigned", "session_id": "worker-1"},
                "b": {"status": "assigned", "session_id": "worker-2"},
            },
        }
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


def test_deadline_result_retains_last_observed_worker_state(tmp_path, monkeypatch):
    m, clock = setup_wait(tmp_path, monkeypatch)
    result = m.wait_for_change(
        lambda *a, **k: {"status": "running"}, 20, clock=clock.time, sleep=clock.sleep
    )
    assert result["observed"]["/sessions/worker-1"]["status"] == "running"
