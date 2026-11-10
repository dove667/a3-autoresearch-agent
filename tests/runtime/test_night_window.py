from datetime import datetime

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
    from types import SimpleNamespace

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
