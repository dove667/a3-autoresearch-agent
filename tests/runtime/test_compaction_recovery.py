"""Context pressure yields to recovery without a wall-clock exit deadline."""

import json
import threading

import pytest

from a3_research_agent.harness import state_paths
from a3_research_agent.harness.context import compaction
from a3_research_agent.harness.context import manager as cm


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setattr(
        cm,
        "TOKEN_CONFIG",
        {
            "decision_input_tokens": 49000,
            "trigger_tokens": 40000,
            "target_tokens": 12000,
            "injected_reserve_tokens": 1000,
            "summary_input_tokens": 491808,
            "summary_reserve_tokens": 5000,
        },
    )
    monkeypatch.setattr(cm, "metric", cm.size)
    return [{"role": "user", "content": "research evidence " * 4000}]


def ready():
    return [{"role": "user", "content": "Source-bound working summary."}], {"source": "saved"}


def test_slow_summary_remains_pending_beyond_three_hundred_seconds(context, monkeypatch):
    clock, entered, release, calls = [0], threading.Event(), threading.Event(), []

    def prepare(*args, **kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        return ready()

    monkeypatch.setattr(cm, "prepare", prepare)
    fork = compaction.ForkCompactor(lambda *a, **k: {}, clock=lambda: clock[0])
    try:
        with pytest.raises(cm.ContextPending):
            fork.before_request(context)
        assert entered.wait(2)
        source = fork.status()["result"]["source"]
        for elapsed in (300, 600, 3600):
            clock[0] = elapsed
            with pytest.raises(cm.ContextPending) as pending:
                fork.before_request(context)
            assert not pending.value.blocked and len(calls) == 1
            assert json.loads(state_paths.path(source).read_text()) == context
        release.set()
        fork.wait(5)
        candidate, report = fork.before_request(context)
        assert report["kind"] == "applied" and cm.message_cost(candidate) < fork._budget()
        assert calls[0]["budget_limit"] == fork._budget()
    finally:
        release.set()
        fork.wait(5)


def test_retry_schedule_survives_restart_and_eventually_applies(context, monkeypatch):
    clock, calls = [0], []

    def prepare(*args, **kwargs):
        calls.append(clock[0])
        if len(calls) <= 6:
            raise cm.SummaryRetry("upstream_unavailable", retry_after=60)
        return ready()

    monkeypatch.setattr(cm, "prepare", prepare)
    fork = compaction.ForkCompactor(lambda *a, **k: {}, clock=lambda: clock[0])
    fork.start(context)
    fork.wait(5)
    for attempt in range(6):
        # A fresh process reads the same due time and cannot spend another call early.
        fork = compaction.ForkCompactor(lambda *a, **k: {}, clock=lambda: clock[0])
        with pytest.raises(cm.ContextPending) as pending:
            fork.before_request(context)
        assert not pending.value.blocked and len(calls) == attempt + 1
        assert pending.value.retry_after == 60
        clock[0] += 60
        with pytest.raises(cm.ContextPending):
            fork.before_request(context)
        fork.wait(5)
    candidate, report = fork.before_request(context)
    assert report["kind"] == "applied" and candidate == ready()[0]
    assert len(calls) == 7 and clock[0] == 360


def test_soft_trigger_retry_does_not_block_a_request_that_still_fits(context, monkeypatch):
    monkeypatch.setattr(
        cm,
        "prepare",
        lambda *a, **k: (_ for _ in ()).throw(
            cm.SummaryRetry("service_unavailable", retry_after=60)
        ),
    )
    messages = [{"role": "user", "content": "small history"}]
    fork = compaction.ForkCompactor(lambda *a, **k: {}, clock=lambda: 0)
    fork.start(messages)
    fork.wait(5)
    unchanged, report = fork.before_request(messages)
    assert unchanged is messages and report["kind"] == "retry_wait"


def test_permanent_error_stays_observable_until_contract_changes(context, monkeypatch):
    calls = []

    def prepare(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise cm.ContextError("summary request rejected by configured provider")
        return ready()

    monkeypatch.setattr(cm, "prepare", prepare)
    fork = compaction.ForkCompactor(lambda *a, **k: {})
    fork.start(context)
    fork.wait(5)
    for _ in range(3):
        with pytest.raises(cm.ContextPending) as pending:
            fork.before_request(context)
        assert pending.value.blocked and len(calls) == 1
    assert fork.status()["result"]["status"] == "blocked"
    monkeypatch.setitem(cm.TOKEN_CONFIG, "model_routes_sha256", "repaired-model-route")
    with pytest.raises(cm.ContextPending):
        fork.before_request(context)
    fork.wait(5)
    candidate, report = fork.before_request(context)
    assert report["kind"] == "applied" and candidate == ready()[0]
    assert len(calls) == 2


def test_shutdown_does_not_restart_due_summary_retry(context, monkeypatch):
    calls = []

    def prepare(*args, **kwargs):
        calls.append(kwargs)
        raise cm.SummaryRetry("service_unavailable", retry_after=1)

    monkeypatch.setattr(cm, "prepare", prepare)
    clock = [0]
    fork = compaction.ForkCompactor(lambda *a, **k: {}, clock=lambda: clock[0])
    fork.start(context)
    fork.wait(5)
    clock[0] = 10
    unchanged, report = fork.apply_ready(context)
    assert unchanged is context and report["kind"] == "retry_wait"
    assert not fork.status()["busy"] and len(calls) == 1


def test_soft_target_does_not_trim_new_observations_with_hard_budget_space(context, monkeypatch):
    monkeypatch.setattr(cm, "prepare", lambda *a, **k: ready())
    monkeypatch.setattr(cm, "fit_tool_outputs", lambda *a, **k: pytest.fail("unneeded fitting"))
    fork = compaction.ForkCompactor(lambda *a, **k: {})
    fork.start(context)
    fork.wait(5)
    newest = [
        {"type": "function_call", "call_id": "new", "name": "read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "new", "output": "fresh evidence " * 1500},
    ]
    candidate, report = fork.before_request(context + newest)
    assert report["kind"] == "applied" and report["soft_target_exceeded"] is True
    assert candidate[-2:] == newest and cm.message_cost(candidate) <= fork._budget()


def test_soft_target_smaller_than_fixed_reserves_can_still_apply(context, monkeypatch):
    monkeypatch.setitem(cm.TOKEN_CONFIG, "target_tokens", 500)
    monkeypatch.setattr(cm, "prepare", lambda *a, **k: ready())
    fork = compaction.ForkCompactor(lambda *a, **k: {})
    fork.start(context)
    fork.wait(5)
    candidate, report = fork.before_request(context)
    assert candidate == ready()[0] and report["soft_target_exceeded"] is True


def test_permission_repair_is_probed_without_contract_changes(context, monkeypatch):
    messages = [{"role": "user", "content": "Small context still fits."}]
    clock, calls = [0], []
    probing, release = threading.Event(), threading.Event()

    def prepare(*args, **kwargs):
        calls.append(clock[0])
        if len(calls) == 1:
            raise cm.SummaryRetry("summary_access_denied", retry_after=60, blocked=True)
        probing.set()
        assert release.wait(5)
        return ready()

    monkeypatch.setattr(cm, "prepare", prepare)
    fork = compaction.ForkCompactor(lambda *a, **k: {}, clock=lambda: clock[0])
    try:
        fork.start(messages)
        fork.wait(5)
        with pytest.raises(cm.ContextPending) as pending:
            fork.before_request(messages)
        assert pending.value.blocked is True and pending.value.retry_after == 60
        assert calls == [0]
        clock[0] = 60
        with pytest.raises(cm.ContextPending) as pending:
            fork.before_request(messages)
        assert pending.value.blocked is True
        assert probing.wait(2)
        with pytest.raises(cm.ContextPending) as pending:
            fork.before_request(messages)
        assert pending.value.blocked is True and calls == [0, 60]
        release.set()
        fork.wait(5)
        candidate, report = fork.before_request(messages)
        assert report["kind"] == "applied" and candidate == ready()[0]
        assert "blocked" not in fork.status()["result"]
    finally:
        release.set()
        fork.wait(5)
