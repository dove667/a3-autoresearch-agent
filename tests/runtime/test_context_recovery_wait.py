"""Context stalls wait in the live process and stay interruptible."""

import http.client
import json
import sys

import pytest

from a3_research_agent.harness import agent, lifecycle, state_paths
from a3_research_agent.harness.context import manager as cm
from a3_research_agent.harness.context import recovery


class Clock:
    def __init__(self):
        self.now = 0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_wait_remains_alive_past_300_seconds_without_research_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path))
    clock, calls = Clock(), []

    def request(method, path, **kwargs):
        calls.append(path)
        return {"http_error": 503}

    for _ in range(12):
        recovery.wait(
            cm.ContextPending("summary pending", retry_after=90),
            request,
            clock=clock.time,
            sleep=clock.sleep,
        )
    assert clock.now == 360
    assert calls and set(calls) == {"/sessions/self"}
    assert (
        json.loads(state_paths.path(state_paths.CONTEXT_FAILURE).read_text())["status"] == "waiting"
    )
    recovery.clear()
    assert not state_paths.path(state_paths.CONTEXT_FAILURE).exists()


def test_blocked_wait_stays_interruptible_even_if_status_storage_is_unavailable(monkeypatch):
    monkeypatch.setattr(cm, "atomic", lambda *args: (_ for _ in ()).throw(OSError("full")))
    calls = []

    def interrupt(seconds):
        calls.append(seconds)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        recovery.wait(
            cm.ContextError("invalid source hash"),
            lambda *args, **kwargs: {"is_main": True},
            sleep=interrupt,
        )
    assert calls == [1]


def test_operator_identity_change_stops_wait_and_temporary_failure_does_not():
    with pytest.raises(cm.ContextPending):
        lifecycle.self_status(lambda *args, **kwargs: {"http_error": 502})
    with pytest.raises(lifecycle.ResearchStopped):
        recovery.wait(cm.ContextPending("pending"), lambda *args, **kwargs: {"is_main": False})


def test_inputs_change_wakes_wait_for_fresh_context_contract(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(recovery, "input_revision", lambda: 1 if clock.now < 2 else 2)
    recovery.wait(
        cm.ContextPending("pending", retry_after=30),
        lambda *args, **kwargs: {"is_main": True},
        clock=clock.time,
        sleep=clock.sleep,
    )
    assert clock.now == 2


def test_truncated_host_response_remains_a_recoverable_wait():
    def request(*args, **kwargs):
        raise http.client.IncompleteRead(b"partial", 100)

    with pytest.raises(cm.ContextPending):
        lifecycle.self_status(request)
    clock = Clock()
    recovery.wait(
        cm.ContextPending("pending", retry_after=3), request, clock=clock.time, sleep=clock.sleep
    )
    assert clock.now == 3


def test_agent_loop_recovers_context_config_and_compaction_without_tool_replay(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("A3_GATEWAY_URL", "http://unused")
    monkeypatch.setenv("A3_GATEWAY_TOKEN", "test-only")
    state_paths.state_root().mkdir(parents=True, exist_ok=True)
    state_paths.path(state_paths.CHECKPOINT).write_text("[]")
    monkeypatch.setattr(sys, "argv", ["agent", "--test"])
    monkeypatch.setattr(lifecycle, "self_status", lambda request: {"is_main": True})
    config_calls, waits, tools, decisions = [], [], [], []

    def configure(request):
        config_calls.append(True)
        if len(config_calls) <= 3:
            raise OSError("temporarily unavailable")

    class Compactor:
        def __init__(self, *args, **kwargs):
            self.calls = 0

        def before_request(self, messages, **kwargs):
            self.calls += 1
            if self.calls <= 12:
                raise cm.ContextPending("summary pending", retry_after=30)
            return messages, None

        def wait(self, timeout):
            pass

        def apply_ready(self, messages):
            return messages, None

    monkeypatch.setattr(agent.compaction, "ForkCompactor", Compactor)
    monkeypatch.setattr(cm, "configure", configure)
    monkeypatch.setattr(agent, "req", lambda *args, **kwargs: {})
    monkeypatch.setattr(agent, "operational_message", lambda: {"role": "user", "content": "state"})
    monkeypatch.setattr(recovery, "wait", lambda error, *args, **kwargs: waits.append(error))
    monkeypatch.setattr(
        agent, "dispatch_tool_call", lambda call: tools.append(call) or {"ok": True}
    )
    monkeypatch.setattr(
        agent,
        "request_model",
        lambda request: (
            decisions.append(request)
            or {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": "job_status",
                        "arguments": '{"job_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
                        "call_id": "once",
                    }
                ],
            }
        ),
    )
    agent.main()
    assert len(waits) == 15 and len(decisions) == len(tools) == 1
    assert state_paths.path(state_paths.JOURNAL).read_text() == "null"
