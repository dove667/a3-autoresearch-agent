from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from a3_research_agent.host.runs import directories as launch
from runtime.remote.remote_jobs import validate_submit


@pytest.fixture
def fixed_clock(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 31, 11, 4, 15, 123456, tzinfo=tz)

    monkeypatch.setattr(launch, "datetime", FixedDatetime, raising=False)


def test_run_directory_uses_local_timestamp_and_is_a_valid_execution_identity(
    tmp_path, fixed_clock
):
    run = launch.create_run_directory(tmp_path / "rsi")

    assert run.name == "run-20261031-110415-123456"
    assert run.is_dir()
    validate_submit({"experiment_id": run.name, "command": ["true"], "gpu": False})


def test_simultaneous_runs_never_share_or_overwrite_a_directory(tmp_path, fixed_clock):
    first = launch.create_run_directory(tmp_path / "rsi")
    evidence = first / "evidence.txt"
    evidence.write_text("preserve this run")

    with ThreadPoolExecutor(max_workers=8) as pool:
        runs = list(pool.map(lambda _: launch.create_run_directory(first.parent), range(16)))

    assert len(set(runs + [first])) == 17
    assert all(run.is_dir() for run in runs)
    assert evidence.read_text() == "preserve this run"
