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
        {
            "http_error": 400,
            "model_error": "upstream_bad_request",
            "gateway_request_id": "first-request",
        },
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


def test_model_recovers_past_four_failures_with_capped_backoff(monkeypatch):
    response = {"status": "completed"}
    transport = Mock(side_effect=[TimeoutError("slow"), {"http_error": 502}] * 4 + [response])
    sleep = Mock()
    monkeypatch.setattr(agent, "req", transport)
    monkeypatch.setattr(agent.time, "sleep", sleep)
    monkeypatch.setattr(agent, "log", Mock())
    assert agent.request_model({}) == response
    assert transport.call_count == 9
    assert [c.args[0] for c in sleep.call_args_list] == [5, 10, 20, 40, 80, 120, 120, 120]


@pytest.mark.parametrize("status", [400, 401, 403, 413, 422])
def test_permanent_http_failures_do_not_retry(monkeypatch, status):
    response = {"http_error": status}
    transport = Mock(return_value=response)
    monkeypatch.setattr(agent, "req", transport)
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        agent.request_model({})
    transport.assert_called_once()


def test_model_retry_wait_can_be_interrupted(monkeypatch):
    transport = Mock(return_value={"http_error": 503})
    monkeypatch.setattr(agent, "req", transport)
    monkeypatch.setattr(agent.time, "sleep", Mock(side_effect=KeyboardInterrupt))
    monkeypatch.setattr(agent, "log", Mock())
    with pytest.raises(KeyboardInterrupt):
        agent.request_model({})
    assert transport.call_count == 1


def test_upstream_bad_request_retries_twice_and_preserves_final_request_id(monkeypatch):
    request = {"input": [{"role": "user", "content": "continue"}]}
    transport = Mock(
        side_effect=[
            {"http_error": 400, "model_error": "upstream_bad_request", "gateway_request_id": str(n)}
            for n in range(3)
        ]
    )
    sleep, log = Mock(), Mock()
    monkeypatch.setattr(agent, "req", transport)
    monkeypatch.setattr(agent.time, "sleep", sleep)
    monkeypatch.setattr(agent, "log", log)

    with pytest.raises(agent.ModelRequestRejected, match="HTTP 400") as rejected:
        agent.request_model(request)

    assert rejected.value.gateway_request_id == "2"
    assert transport.call_count == 3
    assert all(call.args[2] is request for call in transport.call_args_list)
    assert [call.args[0] for call in sleep.call_args_list] == [5, 10]
    assert [call.args[0]["gateway_request_id"] for call in log.call_args_list] == ["0", "1"]


def test_forced_tool_choice_rejection_falls_back_without_same_request_retry(monkeypatch):
    transport = Mock(return_value={"http_error": 400, "model_error": "upstream_bad_request"})
    monkeypatch.setattr(agent, "req", transport)
    with pytest.raises(agent.ModelRequestRejected, match="HTTP 400"):
        agent.request_model({"tool_choice": "required"})
    transport.assert_called_once()


def test_job_submission_timeout_is_not_retried(monkeypatch):
    transport = Mock(side_effect=TimeoutError())
    monkeypatch.setattr(agent, "BASE", "http://gateway")
    monkeypatch.setattr(agent, "TOKEN", "test-token")
    monkeypatch.setattr(agent._OPENER, "open", transport)
    with pytest.raises(TimeoutError):
        agent.req("POST", "/jobs", {"command": ["python", "train.py"]})
    transport.assert_called_once()
