"""A run stops between turns when the operator asks, without interrupting a call."""

import json

import pytest


@pytest.fixture(autouse=True)
def state_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path))


def test_no_request_means_no_stop():
    from a3_research_agent.harness import stop_request

    assert stop_request.pending() is None
    assert stop_request.take() is None


def test_a_requested_stop_is_consumed_once():
    from a3_research_agent.harness import stop_request

    stop_request.request("moving the laptop")
    assert stop_request.pending()["reason"] == "moving the laptop"
    taken = stop_request.take()
    assert taken["reason"] == "moving the laptop"
    assert stop_request.take() is None


def test_an_unreadable_request_still_stops_the_run():
    from a3_research_agent.harness import stop_request

    stop_request.path().parent.mkdir(parents=True, exist_ok=True)
    stop_request.path().write_text("{not json")
    taken = stop_request.take()
    assert taken is not None and "unreadable" in taken["reason"]


def test_the_cli_writes_into_the_run_conversation(tmp_path):
    from a3_research_agent.cli import request_stop

    run = tmp_path / "run"
    (run / "conversation").mkdir(parents=True)
    argv = ["request_stop", str(run), "--reason", "batch finished"]
    original = __import__("sys").argv
    __import__("sys").argv = argv
    try:
        request_stop.main()
    finally:
        __import__("sys").argv = original
    written = json.loads((run / "conversation" / "stop-request.json").read_text())
    assert written["reason"] == "batch finished"
