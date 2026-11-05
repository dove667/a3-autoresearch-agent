"""Transient model failures retry before any returned tool call is executed."""

import urllib.error
from unittest.mock import Mock

import pytest

from a3_research_agent.harness import agent


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        ConnectionResetError(),
        urllib.error.URLError("offline"),
        {"http_error": 502},
        {"http_error": 429},
    ],
)
def test_model_request_recovers_with_same_input(monkeypatch, failure):
    request = {"input": [{"role": "user", "content": "continue"}]}
    response = {"status": "completed", "output": [{"type": "function_call", "name": "gateway"}]}
    transport = Mock(side_effect=[failure, response])
    sleep, log = Mock(), Mock()
    monkeypatch.setattr(agent, "req", transport)
    monkeypatch.setattr(agent.time, "sleep", sleep)
    monkeypatch.setattr(agent, "log", log)
    assert agent.request_model(request) == response
    assert transport.call_count == 2
    assert all(c.args == ("POST", "/v1/responses", request) for c in transport.call_args_list)
    assert all(c.kwargs["timeout"] > 900 for c in transport.call_args_list)
    sleep.assert_called_once_with(5)
    assert "input" not in log.call_args.args[0]


def test_model_timeout_exhaustion_remains_recoverable(monkeypatch):
    transport = Mock(side_effect=TimeoutError("slow"))
    sleep = Mock()
    monkeypatch.setattr(agent, "req", transport)
    monkeypatch.setattr(agent.time, "sleep", sleep)
    monkeypatch.setattr(agent, "log", Mock())
    with pytest.raises(TimeoutError):
        agent.request_model({})
    assert transport.call_count == 4
    assert [c.args[0] for c in sleep.call_args_list] == [5, 10, 20]


@pytest.mark.parametrize("status", [400, 401, 403, 413, 422])
def test_permanent_http_failures_do_not_retry(monkeypatch, status):
    response = {"http_error": status}
    transport = Mock(return_value=response)
    monkeypatch.setattr(agent, "req", transport)
    assert agent.request_model({}) == response
    transport.assert_called_once()


def test_http_retry_exhaustion_stops_instead_of_entering_empty_response_loop(monkeypatch):
    transport = Mock(return_value={"http_error": 503})
    monkeypatch.setattr(agent, "req", transport)
    monkeypatch.setattr(agent.time, "sleep", Mock())
    monkeypatch.setattr(agent, "log", Mock())
    with pytest.raises(RuntimeError, match="HTTP 503"):
        agent.request_model({})
    assert transport.call_count == 4


def test_job_submission_timeout_is_not_retried(monkeypatch):
    transport = Mock(side_effect=TimeoutError())
    monkeypatch.setattr(agent, "BASE", "http://gateway")
    monkeypatch.setattr(agent, "TOKEN", "test-token")
    monkeypatch.setattr(agent.urllib.request, "urlopen", transport)
    with pytest.raises(TimeoutError):
        agent.req("POST", "/jobs", {"command": ["python", "train.py"]})
    transport.assert_called_once()
