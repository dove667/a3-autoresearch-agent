import json
import os

import pytest

from a3_research_agent.host.runs.state import RunLease, RunStateStore


def test_host_state_is_private_and_atomic(tmp_path):
    store = RunStateStore(tmp_path)
    store.save({"token": "private", "generation": 1})
    assert store.load()["generation"] == 1
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    store.save({"token": "private", "generation": 2})
    assert json.loads(store.path.read_text())["generation"] == 2
    assert not store.path.with_suffix(".tmp").exists()


def test_same_run_cannot_have_two_hosts(tmp_path):
    with (
        RunLease(tmp_path),
        pytest.raises(RuntimeError, match="already running"),
        RunLease(tmp_path),
    ):
        pass
    with RunLease(tmp_path):
        pass
