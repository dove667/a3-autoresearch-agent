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


def test_scheduler_does_not_restart_an_exited_host(tmp_path, monkeypatch):
    from a3_research_agent.cli import night_window
    from a3_research_agent.host.runs.state import RunStateStore

    now = datetime(2026, 11, 7, 22, tzinfo=BEIJING)
    monkeypatch.setattr(night_window, "datetime", SimpleNamespace(now=lambda _: now))
    launches = []

    class Host:
        returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def poll(self):
            return 0

    def spawn(command, **kwargs):
        launches.append(command)
        return Host()

    monkeypatch.setattr(night_window.subprocess, "Popen", spawn)
    run = tmp_path / "run"
    schedule = SimpleNamespace(select=lambda: (["resume-selected-run"], run, {}))
    assert night_window.run_nightly(["host"], RunStateStore(run), schedule=schedule) == 0
    assert launches == [["resume-selected-run"]]


def test_start_failure_retries_with_backoff_and_discards_the_empty_run(tmp_path, monkeypatch):
    from a3_research_agent.cli import night_window

    now = datetime(2026, 11, 7, 22, tzinfo=BEIJING)
    monkeypatch.setattr(night_window, "datetime", SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(night_window, "STARTUP_RETRY_DELAYS", (0,))
    runs = []
    launches = []

    def select():
        run = tmp_path / f"run-{len(runs)}"
        run.mkdir()
        runs.append(run)
        return ["host"], run, {"A3_NIGHT_WINDOW_RUN_DIR": str(run)}

    class Host:
        def __init__(self, returncode):
            self.returncode = returncode

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def poll(self):
            return self.returncode

    def spawn(command, **kwargs):
        launches.append(command)
        return Host(3 if len(launches) == 1 else 0)

    monkeypatch.setattr(night_window.subprocess, "Popen", spawn)
    assert night_window.run_nightly(["host"], None, schedule=SimpleNamespace(select=select)) == 0
    assert launches == [["host"], ["host"]]
    assert [run.exists() for run in runs] == [False, True]


def test_exited_host_after_run_state_is_not_restarted(tmp_path, monkeypatch):
    from a3_research_agent.cli import night_window
    from a3_research_agent.host.runs.state import RunStateStore

    now = datetime(2026, 11, 7, 22, tzinfo=BEIJING)
    monkeypatch.setattr(night_window, "datetime", SimpleNamespace(now=lambda _: now))
    launches = []

    class Host:
        returncode = 2

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def poll(self):
            return self.returncode

    def spawn(command, **kwargs):
        launches.append(command)
        return Host()

    monkeypatch.setattr(night_window.subprocess, "Popen", spawn)
    run = tmp_path / "run"
    run.mkdir()
    RunStateStore(run).save({"status": "starting"})
    schedule = SimpleNamespace(select=lambda: (["host"], run, {"A3_NIGHT_WINDOW_RUN_DIR": str(run)}))
    assert night_window.run_nightly(["host"], None, schedule=schedule) == 2
    assert launches == [["host"]]


def test_startup_retry_delay_grows_and_stops_at_the_cap():
    from a3_research_agent.cli.night_window import STARTUP_RETRY_DELAYS, startup_retry_delay

    delays = [startup_retry_delay(attempt) for attempt in range(1, 8)]
    assert delays == [*STARTUP_RETRY_DELAYS, STARTUP_RETRY_DELAYS[-1], STARTUP_RETRY_DELAYS[-1]]


def test_each_night_selects_a_fresh_trajectory_without_old_context(tmp_path):
    from a3_research_agent.cli.night_window import FreshNightSchedule

    command = ["host", "--harness-commit", "a" * 40]
    schedule = FreshNightSchedule(tmp_path, command)
    first_command, first, first_env = schedule.select()
    (first / "conversation").mkdir()
    (first / "conversation/checkpoint.json").write_text('[{"role":"user","content":"yesterday"}]')
    second_command, second, second_env = schedule.select()
    assert first != second
    assert first_command == second_command == command
    assert "--resume-from" not in second_command
    assert not list(second.iterdir())
    assert first_env["A3_NIGHT_WINDOW_RUN_DIR"] == str(first)
    assert second_env["A3_NIGHT_WINDOW_RUN_DIR"] == str(second)



def test_window_end_hour_is_configurable():
    from a3_research_agent.cli import night_window

    assert night_window.window_boundary(datetime(2026, 11, 7, 8, 0, tzinfo=BEIJING), 7) == (
        False,
        datetime(2026, 11, 7, 21, 0, tzinfo=BEIJING),
    )
    assert night_window.window_boundary(datetime(2026, 11, 8, 6, 59, tzinfo=BEIJING), 7) == (
        True,
        datetime(2026, 11, 8, 7, 0, tzinfo=BEIJING),
    )
    assert night_window.window_boundary(datetime(2026, 11, 7, 22, 0, tzinfo=BEIJING), 7) == (
        True,
        datetime(2026, 11, 8, 7, 0, tzinfo=BEIJING),
    )


def test_window_end_hour_comes_from_the_environment(monkeypatch):
    from a3_research_agent.cli import night_window

    monkeypatch.delenv("A3_NIGHT_WINDOW_END_HOUR", raising=False)
    assert night_window.end_hour() == 9
    monkeypatch.setenv("A3_NIGHT_WINDOW_END_HOUR", "7")
    assert night_window.end_hour() == 7


def test_window_end_hour_must_leave_a_window(monkeypatch):
    from a3_research_agent.cli import night_window

    for value in ("21", "-1", "later"):
        monkeypatch.setenv("A3_NIGHT_WINDOW_END_HOUR", value)
        with pytest.raises(ValueError):
            night_window.end_hour()
