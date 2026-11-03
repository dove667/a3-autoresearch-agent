"""Synthetic scheduling only: no provider, real conversation or remote job."""

import importlib.util
import itertools
import json
import pathlib
import sys
import threading

import pytest


def load_client(monkeypatch):
    root = pathlib.Path(__file__).resolve().parents[2] / "runtime/harness_recovery"
    spec = importlib.util.spec_from_file_location("context_manager", root / "context_manager.py")
    context = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(context)
    monkeypatch.setitem(sys.modules, "context_manager", context)
    spec = importlib.util.spec_from_file_location(
        "analyzer_client_test", root / "analyzer_client.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rig(tmp_path, monkeypatch):
    module = load_client(monkeypatch)
    clock = [1000]
    stats = {"decisions": 0, "progress_updates": 0, "repeated_static_reads": 0}
    remote = {"session_id": "main", "main_owner": "main", "latest": {"status": "not_requested"}}
    calls = []

    def req(method, path, data=None, *, timeout):
        assert timeout == 2
        calls.append((method, path, data))
        if path == "/sessions/self":
            return {"session_id": remote["session_id"]}
        if path == "/evolution":
            return {"main_owner": remote["main_owner"]}
        if method == "POST":
            assert path == "/analysis"
            if data.get("enqueue_only"):
                return {"status": "queued"}
            assert json.loads((tmp_path / "schedule.json").read_text())["last_attempt"] == clock[0]
            if remote.get("fail"):
                raise TimeoutError("synthetic failure")
            return remote.get("post", {"status": "running"})
        return remote["latest"]

    def make(**kwargs):
        return module.Controller(
            req,
            lambda: dict(stats),
            clock=lambda: clock[0],
            path=tmp_path / "schedule.json",
            **kwargs,
        )

    return module, clock, stats, remote, calls, make


def posts(calls):
    return [
        call[2]["trigger"]
        for call in calls
        if call[0] == "POST" and not call[2].get("enqueue_only")
    ]


def test_periodic_runs_without_repetition_or_stagnation_and_during_training_wait(rig):
    _, now, stats, _, calls, make = rig
    stats["waiting"] = True
    client = make()
    for minute in range(31):
        now[0] = 1000 + minute * 60
        stats["decisions"] += 3
        client.poll()
    assert posts(calls) == ["periodic"]


def test_normal_progress_suppresses_stagnation_but_never_omits_periodic(rig):
    _, now, stats, _, calls, make = rig
    client = make()
    for minute in range(31):
        now[0] = 1000 + minute * 60
        stats["decisions"] += 5
        stats["progress_updates"] += 1
        client.poll()
    assert posts(calls) == ["periodic"]


def test_no_progress_needs_both_twenty_decisions_and_ten_minutes(rig):
    _, now, stats, _, calls, make = rig
    client = make()
    client.poll()
    stats["decisions"] = 20
    now[0] += 540
    client.poll()
    assert posts(calls) == []
    now[0] += 60
    client.poll()
    assert posts(calls) == ["no_progress"]


def test_waiting_callback_resets_stagnation_baseline(rig):
    _, now, stats, _, calls, make = rig
    waiting = [True]
    client = make(waiting=lambda: waiting[0])
    client.poll()
    now[0] += 600
    stats["decisions"] = 100
    client.poll()
    waiting[0] = False
    now[0] += 60
    client.poll()
    assert posts(calls) == []


def test_repeated_same_content_ranges_trigger_even_with_recorded_progress(rig):
    _, now, stats, _, calls, make = rig
    client = make()
    client.poll()
    for minute in range(1, 6):
        now[0] += 60
        stats["repeated_static_reads"] += 2
        stats["progress_updates"] += 1
        client.poll()
    assert posts(calls) == ["repetition"]


def test_changed_content_reads_and_old_window_do_not_trigger(rig):
    _, now, stats, _, calls, make = rig
    client = make()
    client.poll()
    stats["repeated_reads"] = 100  # Explicit static counter excludes changing content.
    now[0] += 660
    stats["repeated_static_reads"] = 10
    client.poll()
    assert posts(calls) == []


def test_worker_never_requests_or_reads_analysis_and_can_later_become_main(rig):
    _, now, _, remote, calls, make = rig
    remote["session_id"] = "worker"
    client = make()
    client.poll()
    now[0] += 1800
    client.poll()
    assert not any(path == "/analysis" for _, path, _ in calls)
    remote["main_owner"] = "worker"
    now[0] += 60
    client.poll()
    now[0] += 1800
    client.poll()
    assert posts(calls) == ["periodic"]


def test_failed_post_and_restart_share_cooldown_across_all_triggers(rig):
    _, now, stats, remote, calls, make = rig
    client = make()
    client.poll()
    remote["fail"] = True
    client.trigger("before_handoff")
    assert posts(calls) == ["before_handoff"]
    client = make()
    for _ in range(9):
        now[0] += 60
        stats["repeated_static_reads"] += 10
        client.poll()
        client.trigger("before_handoff")
    assert posts(calls) == ["before_handoff"]
    now[0] += 60
    client.trigger("before_handoff")
    assert posts(calls) == ["before_handoff", "before_handoff"]


def test_inflight_analysis_does_not_spawn_duplicate_even_after_cooldown(rig):
    _, now, _, remote, calls, make = rig
    remote["latest"] = {"status": "running"}
    client = make()
    client.trigger("before_handoff")
    now[0] += 1800
    for _ in range(100):
        client.poll()
        client.trigger("before_handoff")
    assert posts(calls) == []
    assert len(calls) <= 12


def test_advisory_view_requires_explicit_response_and_never_applies_suggestions(rig):
    _, _, _, remote, calls, make = rig
    remote["latest"] = {
        "status": "completed",
        "analysis_id": "a" * 32,
        "suggestions": [{"action": "delete evidence"}],
    }
    client = make()
    client.poll()
    view = client.view()
    assert view["advisory_only"] and view["response_required"]
    assert view["response_contract"]["decision"] == ["adopt", "reject", "defer"]
    assert view["response_contract"]["reason_required"]
    view["suggestions"].clear()
    assert client.view()["suggestions"]
    assert posts(calls) == []


def test_provider_http_failure_does_not_raise_or_call_model_endpoint(rig):
    _, _, _, remote, calls, make = rig
    remote["post"] = {"http_error": 503}
    client = make()
    assert client.trigger("before_handoff") is not None
    assert all(path in {"/analysis", "/sessions/self", "/evolution"} for _, path, _ in calls)
    with pytest.raises(ValueError):
        client.trigger("manual")


def test_damaged_schedule_is_preserved_and_blocks_client_calls(rig, tmp_path):
    _, _, _, _, calls, make = rig
    path = tmp_path / "schedule.json"
    path.write_text("{broken")
    client = make()
    assert client.poll() == {
        "status": "disabled",
        "reason": "invalid_persisted_schedule",
        "advisory_only": True,
        "response_required": False,
    }
    assert calls == [] and path.read_text() == "{broken"


def test_background_network_does_not_block_view_or_other_triggers(rig, tmp_path):
    module, now, stats, _, _, _ = rig
    entered, release = threading.Event(), threading.Event()

    def slow_request(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        raise TimeoutError("synthetic timeout")

    client = module.Controller(
        slow_request, lambda: stats, clock=lambda: now[0], path=tmp_path / "schedule.json"
    )
    # Avoid any local file access in this concurrency-only check.
    client._save = lambda: None
    thread = threading.Thread(target=client.poll)
    thread.start()
    try:
        assert entered.wait(2)
        assert client.view() is None
        assert client.trigger("before_handoff") is None
        assert thread.is_alive()
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_recorded_response_does_not_request_another_adoption_decision(rig):
    _, _, _, remote, _, make = rig
    remote["latest"] = {
        "status": "completed",
        "analysis_id": "a" * 32,
        "response": {"decision": "defer", "reason": "Pending independent validation"},
    }
    client = make()
    client.poll()
    assert not client.view()["response_required"]
    assert "response_contract" not in client.view()


def test_older_pending_report_requires_response_even_when_latest_responded(rig):
    _, _, _, remote, calls, make = rig
    remote["latest"] = {
        "status": "completed",
        "analysis_id": "b" * 32,
        "response": {"decision": "reject", "reason": "Already reviewed"},
        "pending_reports": [
            {"analysis_id": "a" * 32, "status": "completed", "suggestions": []},
            {"analysis_id": "c" * 32, "status": "completed", "suggestions": []},
        ],
        "omitted_pending_reports": 2,
        "history": {"evicted_reports": 1, "evicted_pending_reports": 0},
    }
    client = make()
    client.poll()
    view = client.view()
    assert view["response_required"]
    assert view["response_contract"]["analysis_id"] == "a" * 32
    assert view["omitted_pending_reports"] == 2
    assert view["history"]["evicted_reports"] == 1
    assert posts(calls) == []


def test_starting_new_analysis_keeps_existing_unanswered_backlog_visible(rig):
    _, _, _, remote, calls, make = rig
    remote["latest"] = {
        "status": "completed",
        "analysis_id": "a" * 32,
        "pending_reports": [{"analysis_id": "a" * 32, "status": "completed"}],
        "omitted_pending_reports": 0,
    }
    client = make()
    client.trigger("before_handoff")
    assert posts(calls) == ["before_handoff"]
    assert client.view()["status"] == "running"
    assert client.view()["response_contract"]["analysis_id"] == "a" * 32


def test_start_is_daemon_with_sixty_second_polling(rig, monkeypatch, tmp_path):
    module, _, _, _, _, _ = rig
    monkeypatch.chdir(tmp_path)
    threads = []

    class Thread:
        def __init__(self, **kwargs):
            threads.append(kwargs)

        def start(self):
            pass

    monkeypatch.setattr(module.threading, "Thread", Thread)
    client = module.start(lambda *args, **kwargs: {}, dict)
    assert threads[0]["daemon"] and threads[0]["target"] == client._run
    assert module.POLL_SECONDS == 60
    client.close()


def test_corrupt_valid_json_advisory_disables_without_requests(tmp_path, monkeypatch):
    analyzer_client = load_client(monkeypatch)

    path = tmp_path / "state.json"
    raw = json.dumps({"version": 1, "samples": [], "latest": "invalid report"})
    path.write_text(raw)
    calls = []
    controller = analyzer_client.Controller(lambda *a, **k: calls.append(a), dict, path=path)
    assert controller.poll()["status"] == "disabled" and not calls
    assert path.read_text() == raw


def test_periodic_failure_remains_due_and_retries_after_backoff(rig):
    _, now, _, remote, calls, make = rig
    client = make()
    client.poll()
    baseline = client.state.get("last_periodic")
    now[0] += 1800
    remote["post"] = {"status": "failed", "analysis_id": "f" * 32}
    client.poll()
    assert client.state.get("last_periodic") == baseline
    remote["latest"] = remote["post"]
    client = make()
    now[0] += 540
    client.poll()
    assert posts(calls) == ["periodic"]
    now[0] += 60
    remote["post"] = {"status": "completed", "analysis_id": "a" * 32}
    client.poll()
    assert posts(calls) == ["periodic", "periodic"]
    assert client.state["last_periodic"] == now[0]


def test_single_handoff_during_cooldown_survives_restart_and_is_drained(rig):
    _, now, _, remote, calls, make = rig
    remote["post"] = {"status": "completed", "analysis_id": "a" * 32}
    client = make()
    client.trigger("before_handoff")
    now[0] += 60
    client.trigger("before_handoff")
    client = make()
    now[0] += 540
    client.poll()
    assert posts(calls) == ["before_handoff", "before_handoff"]


def test_pending_repetition_does_not_expire_while_analysis_runs(rig):
    _, now, stats, remote, calls, make = rig
    client = make()
    client.poll()
    remote["latest"] = {"status": "running", "analysis_id": "a" * 32}
    now[0] += 60
    stats["repeated_static_reads"] = 10
    client.poll()
    now[0] += 660
    remote["latest"] = {"status": "completed", "analysis_id": "a" * 32}
    client = make()
    client.poll()
    assert posts(calls) == ["repetition"]


def test_handoff_event_coalesces_and_does_not_replace_due_periodic(rig):
    _, now, _, remote, calls, make = rig
    client = make()
    client.poll()
    now[0] += 1800
    remote["latest"] = {"status": "running", "analysis_id": "a" * 32}
    for _ in range(50):
        client.trigger("before_handoff")
    assert posts(calls) == []
    remote["latest"] = {"status": "completed", "analysis_id": "a" * 32}
    remote["post"] = {"status": "completed", "analysis_id": "b" * 32}
    now[0] += 60
    client = make()
    client.poll()
    assert posts(calls) == ["periodic"]
    assert client.state["last_periodic"] == now[0]
    now[0] += 600
    client.poll()
    assert len(posts(calls)) == 1


def test_persistent_failure_uses_capped_backoff_without_false_periodic_success(rig):
    _, now, _, remote, calls, make = rig
    client = make()
    client.poll()
    now[0] += 1800
    remote["post"] = {"status": "failed", "analysis_id": "f" * 32}
    remote["latest"] = remote["post"]
    attempt_times = []
    for _ in range(241):
        before = len(posts(calls))
        client = make()
        client.poll()
        if len(posts(calls)) > before:
            attempt_times.append(now[0])
        now[0] += 60
    assert len(attempt_times) <= 7
    assert [b - a for a, b in itertools.pairwise(attempt_times)][:4] == [600, 1200, 2400, 3600]
    assert client.state.get("last_periodic") is None


def test_single_handoff_notice_persists_even_when_background_lock_is_busy(rig):
    _, now, _, remote, calls, make = rig
    client = make()
    held, release = threading.Event(), threading.Event()

    def hold_lock():
        with client.lock:
            held.set()
            assert release.wait(2)

    worker = threading.Thread(target=hold_lock)
    worker.start()
    try:
        assert held.wait(2)
        client.trigger("before_handoff")
        assert posts(calls) == []
    finally:
        release.set()
        worker.join(2)
    remote["post"] = {"status": "completed", "analysis_id": "a" * 32}
    client = make()
    now[0] += 60
    client.poll()
    assert posts(calls) == ["before_handoff"]


def test_successor_reads_host_queued_handoff_without_old_private_report(rig):
    _, _, _, remote, calls, make = rig
    remote["latest"] = {"status": "not_requested", "deferred_triggers": ["before_handoff"]}
    remote["post"] = {"status": "completed", "analysis_id": "b" * 32}
    remote["session_id"] = remote["main_owner"] = "successor"
    client = make()
    client.poll()
    assert posts(calls) == ["before_handoff"]
    assert not any(path.startswith("/analysis/") for _, path, _ in calls)


def test_new_handoff_arriving_during_analysis_is_not_acknowledged_as_old(rig):
    _, now, _, remote, calls, make = rig
    remote["post"] = {"status": "running", "analysis_id": "a" * 32}
    client = make()
    client.trigger("before_handoff")
    now[0] += 60
    remote["latest"] = remote["post"]
    client.trigger("before_handoff")
    now[0] += 60
    remote["latest"] = {"status": "completed", "analysis_id": "a" * 32}
    client.poll()
    now[0] += 480
    remote["post"] = {"status": "completed", "analysis_id": "b" * 32}
    client = make()
    client.poll()
    assert posts(calls) == ["before_handoff", "before_handoff"]


def test_async_periodic_completion_updates_clock_only_after_observed_success(rig):
    _, now, _, remote, calls, make = rig
    client = make()
    client.poll()
    now[0] += 1800
    remote["post"] = {"status": "running", "analysis_id": "a" * 32}
    client.poll()
    assert client.state["last_periodic"] is None
    now[0] += 60
    remote["latest"] = remote["post"]
    client = make()
    client.poll()
    assert client.state["last_periodic"] is None
    remote["latest"] = {"status": "completed", "analysis_id": "a" * 32}
    now[0] += 60
    client.poll()
    assert client.state["last_periodic"] == now[0]
    assert posts(calls) == ["periodic"]


def test_restart_of_reserved_post_without_id_is_reconciled_not_permanently_stuck(rig):
    _, now, _, remote, calls, make = rig
    client = make()
    client.poll()
    client.state["pending"] = {"periodic": now[0]}
    client.state["attempt"] = {"reasons": {"periodic": now[0]}, "analysis_id": None}
    client.state["last_attempt"] = now[0]
    client._save()  # Simulate crash after reservation, before the returned ID was saved.
    now[0] += 60
    client = make()
    client.poll()
    assert client.state["attempt"] is None
    assert "periodic" in client.state["pending"]
    assert client.state["last_periodic"] is None
    now[0] += 600
    remote["post"] = {"status": "completed", "analysis_id": "c" * 32}
    client.poll()
    assert posts(calls) == ["periodic"]
    assert client.state["last_periodic"] == now[0]


def test_evicted_reserved_report_does_not_block_future_periodic_checks(rig):
    module, now, stats, remote, calls, make = rig
    client = make()
    client.poll()
    client.state["pending"] = {"periodic": now[0]}
    client.state["attempt"] = {"reasons": {"periodic": now[0]}, "analysis_id": "a" * 32}
    client.state["last_attempt"] = now[0]
    client._save()
    base_request = client.req

    def request(method, path, data=None, **options):
        if path == "/analysis/" + "a" * 32:
            return {"http_error": 404}
        return base_request(method, path, data, **options)

    now[0] += 60
    client = module.Controller(request, lambda: stats, clock=lambda: now[0], path=client.path)
    remote["latest"] = {"status": "completed", "analysis_id": "b" * 32}
    client.poll()
    assert client.state["attempt"] is None
    assert client.state["last_periodic"] is None
    now[0] += 600
    remote["post"] = {"status": "completed", "analysis_id": "c" * 32}
    client.poll()
    assert posts(calls) == ["periodic"]
