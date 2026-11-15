import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from a3_research_agent.cli.night_window import BEIJING, window_boundary


@pytest.mark.parametrize(
    ("hour", "minute", "running", "boundary_day", "boundary_hour"),
    [
        (8, 59, True, 7, 9),
        (9, 0, False, 7, 21),
        (20, 59, False, 7, 21),
        (21, 0, True, 8, 9),
        (23, 59, True, 8, 9),
        (0, 0, True, 7, 9),
    ],
)
def test_beijing_window(hour, minute, running, boundary_day, boundary_hour):
    now = datetime(2026, 11, 7, hour, minute, tzinfo=BEIJING)
    assert window_boundary(now) == (
        running,
        datetime(2026, 11, boundary_day, boundary_hour, tzinfo=BEIJING),
    )


def test_scheduler_suspends_host_and_resumes_next_window(monkeypatch):
    from a3_research_agent.cli import night_window

    times = iter(
        datetime(2026, 11, day, hour, tzinfo=BEIJING)
        for day, hour in [(7, 8), (7, 9), (7, 9), (7, 21), (7, 21)]
    )
    monkeypatch.setattr(night_window, "datetime", SimpleNamespace(now=lambda _: next(times)))
    signals = []
    launches = []

    class Host:
        returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def poll(self):
            return None if len(launches) == 1 else self.returncode

        def send_signal(self, signum):
            signals.append(signum)

    def spawn(command, *, start_new_session, env):
        assert start_new_session
        assert env["A3_NIGHT_WINDOW_CHILD"] == "1"
        launches.append(command)
        return Host()

    monkeypatch.setattr(night_window.subprocess, "Popen", spawn)
    state = SimpleNamespace(load=lambda: {"status": "suspended"})
    assert night_window.run_nightly(["host"], state) == 0
    assert signals == [night_window.signal.SIGINT]
    assert launches == [["host"], ["host"]]


@pytest.mark.parametrize("status", [502, 422])
def test_scheduler_recovers_same_run_after_upstream_exhaustion(tmp_path, monkeypatch, status):
    from a3_research_agent.cli import night_window
    from a3_research_agent.host.runs.state import RunStateStore

    run = tmp_path / "current"
    state = RunStateStore(run)
    state.save({"status": "suspended", "gateway": {"evolution": {"main_owner": "main"}}})
    workspace = run / "candidates/main/workspace"
    workspace.mkdir(parents=True)
    (workspace / "checkpoint.json").write_text("[]")
    (workspace / "loop-failure.json").write_text(
        json.dumps({"phase": "model_request", "loop_error": "RuntimeError", "time": 140})
    )
    (run / "gateway-events.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "event": "finished",
                    "route": "/v1/responses",
                    "experiment_id": "main",
                    "error_layer": "model_provider",
                    "status_code": status,
                    "time": 130 + attempt,
                }
            )
            + "\n"
            for attempt in range(4)
        )
    )
    now = datetime(2026, 11, 7, 22, tzinfo=BEIJING)
    monkeypatch.setattr(night_window, "datetime", SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(night_window.time, "time", iter([100, 150, 151]).__next__)
    waits = []
    monkeypatch.setattr(
        night_window.threading,
        "Event",
        lambda: SimpleNamespace(is_set=lambda: False, wait=waits.append),
    )
    launches = []

    class Host:
        returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def poll(self):
            return self.returncode

    def spawn(command, **_):
        launches.append(command)
        return Host()

    monkeypatch.setattr(night_window.subprocess, "Popen", spawn)
    recorded = []
    schedule = SimpleNamespace(
        select=lambda: (["start-release"], run, {}), record_started_release=recorded.append
    )
    assert (
        night_window.run_nightly(["host", "--resume-from", "anchor"], state, schedule=schedule) == 0
    )
    if status == 502:
        assert recorded == [run]
        assert waits == [30]
        assert launches == [["start-release"], ["host", "--resume-from", str(run)]]
    else:
        assert recorded == waits == []
        assert launches == [["start-release"]]
