"""Waiting observes compact state without repeatedly inspecting remote jobs."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from a3_research_agent.execution.controller.store import JobStore
from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.gateway.job_observation import JobStatusCache


def api(tmp_path, remote):
    return TestClient(
        create_app(
            model_name="fixture",
            sessions={"token": "main"},
            remote_call=remote,
            forward_model=lambda **kw: pytest.fail("Waiting must not call a model"),
            usage_log=tmp_path / "usage.jsonl",
            analyzer_enabled=False,
        )
    )


HEADERS = {"Authorization": "Bearer token"}


def test_compact_controller_batch_keeps_retry_nonterminal(tmp_path):
    store = JobStore(tmp_path)
    job = store.create("main", ["true"], False, {})
    job.update(status="failed", retry_pending=True, output="large log", gpu_uuid="private")
    store.save(job)
    result = store.job_statuses({"experiment_id": "main", "job_ids": [job["job_id"]]})
    assert result["jobs"][0]["status"] == "restarting"
    assert "output" not in result["jobs"][0]
    assert "gpu_uuid" not in result["jobs"][0]
    with pytest.raises(PermissionError):
        store.job_statuses({"experiment_id": "other", "job_ids": [job["job_id"]]})
    with pytest.raises(ValueError):
        store.job_statuses({"experiment_id": "main", "job_ids": []})


def test_missing_native_watcher_wakes_observer_without_claiming_completion(tmp_path, monkeypatch):
    from a3_research_agent.execution.controller import processes

    store = JobStore(tmp_path)
    job = store.create("main", ["true"], False, {})
    job.update(status="running", backend="process", created_at=0, process={"pid": 123})
    store.save(job)
    monkeypatch.setattr(processes, "is_running", lambda job: False)
    result = store.job_statuses({"experiment_id": "main", "job_ids": [job["job_id"]]})
    assert result["jobs"][0]["status"] == "observation_error"
    assert result["jobs"][0]["exit_code"] is None
    assert store.load(job["job_id"], "main")["status"] == "running"


def test_wait_batches_and_caches_without_logs_or_hardware(tmp_path):
    calls = []

    def remote(operation, request):
        calls.append((operation, request))
        return {"jobs": [{"job_id": j, "status": "running"} for j in request["job_ids"]]}

    client = api(tmp_path, remote)
    body = {"job_ids": ["first", "second"], "seconds": 0}
    for _ in range(3):
        response = client.post("/jobs/wait", headers=HEADERS, json=body)
        assert response.status_code == 200
        assert response.json()["reason"] == "timeout"
        assert len(response.json()["jobs"]) == 2
    assert len(calls) == 1
    assert calls[0][0] == "jobs-status"
    assert client.post("/jobs/wait", json=body).status_code == 401


def test_controller_transport_fault_is_recorded_for_operator_not_returned_to_model(tmp_path):
    def unavailable(*a):
        raise HTTPException(502, "PRIVATE_CONTROLLER_DIAGNOSTIC")

    client = api(tmp_path, unavailable)
    response = client.post("/jobs/wait", headers=HEADERS, json={"job_ids": ["first"], "seconds": 0})
    assert response.status_code == 200
    assert response.json()["reason"] == "timeout"
    assert "PRIVATE_CONTROLLER_DIAGNOSTIC" not in response.text
    assert "502" in (tmp_path / "controller-incidents.jsonl").read_text()


def test_wait_returns_terminal_and_strips_unneeded_fields(tmp_path):
    client = api(
        tmp_path,
        lambda op, req: {
            "jobs": [
                {
                    "job_id": "first",
                    "status": "completed",
                    "exit_code": 0,
                    "output": "NO_LOG_BODY",
                    "gpu_uuid": "NO_GPU",
                }
            ]
        },
    )
    response = client.post(
        "/jobs/wait", headers=HEADERS, json={"job_ids": ["first"], "seconds": 300}
    )
    assert response.json()["reason"] == "terminal"
    assert "NO_LOG_BODY" not in response.text
    assert "NO_GPU" not in response.text


def test_repeated_detailed_queries_share_one_remote_request(tmp_path):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def remote(op, req):
        calls.append(op)
        started.set()
        assert release.wait(5)
        return {"job_id": "first", "status": "running", "output": "retained logs"}

    client = api(tmp_path, remote)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(client.get, "/jobs/first", headers=HEADERS)
        assert started.wait(5)
        two = pool.submit(client.get, "/jobs/first", headers=HEADERS)
        release.set()
        assert one.result().json()["output"] == "retained logs"
        assert two.result().status_code == 200
    assert calls == ["status"]


def test_cancel_invalidates_cached_status(tmp_path):
    state = {"value": "running"}
    calls = []

    def remote(op, req):
        calls.append(op)
        if op == "cancel":
            state["value"] = "cancelled"
        return {"job_id": "first", "status": state["value"]}

    client = api(tmp_path, remote)
    assert client.get("/jobs/first", headers=HEADERS).json()["status"] == "running"
    client.delete("/jobs/first", headers=HEADERS)
    assert client.get("/jobs/first", headers=HEADERS).json()["status"] == "cancelled"
    assert calls == ["status", "cancel", "status"]


def test_cache_expiry_and_failed_refresh_do_not_hide_state_changes():
    now = [0]
    cache = JobStatusCache(clock=lambda: now[0], capacity=2)
    key = ("summary", "main", "first")
    cache.read_many([key], lambda keys: {key: {"status": "running"}})
    now[0] = 29
    assert (
        cache.read_many([key], lambda keys: pytest.fail("Snapshot still fresh"))[key]["status"]
        == "running"
    )
    now[0] = 30
    with pytest.raises(RuntimeError):
        cache.read_many([key], lambda keys: (_ for _ in ()).throw(RuntimeError("offline")))
    assert (
        cache.read_many([key], lambda keys: {key: {"status": "completed"}})[key]["status"]
        == "completed"
    )


def test_wait_does_not_hold_the_session_lock(tmp_path):
    entered = threading.Event()

    def remote(*args):
        entered.set()
        return {"jobs": [{"job_id": "first", "status": "running"}]}

    client = api(tmp_path, remote)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(
            client.post, "/jobs/wait", headers=HEADERS, json={"job_ids": ["first"], "seconds": 1}
        )
        assert entered.wait(5)
        response = client.get("/sessions", headers=HEADERS)
        assert response.status_code == 200
        assert waiting.result().json()["reason"] == "timeout"
