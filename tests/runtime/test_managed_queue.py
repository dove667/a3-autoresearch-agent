"""Server admission persists jobs while resources are busy, without model retries."""

import json
import sys
from argparse import Namespace

from a3_research_agent.execution.controller import managed_queue, processes, submission
from a3_research_agent.execution.controller.store import JobStore
from a3_research_agent.execution.scheduling import host_resources


def options(root):
    return Namespace(
        root=root,
        backend="process",
        data_root=root,
        tmp_root=root / "temporary",
        process_python=sys.executable,
        models_root=None,
        datasets_root=None,
        model_cache_root=None,
        host_memory_reserve_mb=32,
        host_job_memory_mb=8,
        host_cpu_reserve=1,
        host_job_cpus=2,
        disk_reserve_bytes=64,
        run_storage_bytes=1000000,
        timeout=0,
        max_jobs=0,
        share_gpus=True,
        gpu_memory_mb=512,
        gpu_capacity_mb=20000,
        max_gpus=4,
    )


def request():
    return {
        "experiment_id": "main",
        "run_id": "run-main",
        "gpu": False,
        "command": ["python", "work.py"],
        "files": {"work.py": "print(1)"},
    }


def enqueue(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    monkeypatch.setattr(processes, "validate_data_paths", lambda *a: None)
    monkeypatch.setattr(managed_queue, "ensure_dispatcher", lambda store: None)
    result = managed_queue.enqueue(store, request(), options(tmp_path))
    return store, result


def test_busy_job_keeps_same_durable_id_until_server_admits_it(tmp_path, monkeypatch):
    store, result = enqueue(tmp_path, monkeypatch)
    identifier = result["job_id"]
    assert result["status"] == "queued"
    assert "gpu_uuid" not in result
    calls = []

    def busy(store, payload, args, *, queued_job):
        calls.append(queued_job["job_id"])
        return {"job_id": None, "status": "resource_busy"}

    monkeypatch.setattr(submission, "submit", busy)
    assert managed_queue.dispatch_once(store) == 1
    restored = JobStore(tmp_path)
    assert managed_queue.load_index(restored) == [identifier]
    assert restored.load(identifier, "main")["status"] == "queued"

    def admitted(store, payload, args, *, queued_job):
        job = store.load(queued_job["job_id"], "main")
        job["status"] = "completed"
        store.save(job)
        return {"job_id": job["job_id"], "status": "completed"}

    monkeypatch.setattr(submission, "submit", admitted)
    assert managed_queue.dispatch_once(restored) == 0
    assert restored.load(identifier, "main")["status"] == "completed"
    assert calls == [identifier]


def test_real_admission_reuses_uploaded_workspace_and_creates_one_watcher(tmp_path, monkeypatch):
    store, result = enqueue(tmp_path, monkeypatch)
    monkeypatch.setattr(submission, "active_jobs", lambda store: [])
    monkeypatch.setattr(host_resources, "sample", lambda root: {})
    monkeypatch.setattr(host_resources, "admission", lambda *a: None)
    launched = []
    monkeypatch.setattr(submission.subprocess, "Popen", lambda *a, **kw: launched.append(a))
    assert managed_queue.dispatch_once(store) == 1
    job = store.load(result["job_id"], "main")
    assert job["status"] == "created"
    assert (store.workspace(job) / "work.py").read_text() == "print(1)"
    assert len(launched) == 1
    managed_queue.dispatch_once(store)
    assert len(launched) == 1


def test_cancelled_queued_job_is_never_launched(tmp_path, monkeypatch):
    store, result = enqueue(tmp_path, monkeypatch)
    processes.cancel(store, {"experiment_id": "main", "job_id": result["job_id"]})
    monkeypatch.setattr(
        submission, "submit", lambda *a, **kw: (_ for _ in ()).throw(AssertionError())
    )
    assert managed_queue.dispatch_once(store) == 0


def test_transient_controller_fault_stays_internal_without_losing_queue(tmp_path, monkeypatch):
    store, result = enqueue(tmp_path, monkeypatch)

    def unavailable(*a, **kw):
        raise OSError("scheduler temporarily unavailable")

    monkeypatch.setattr(submission, "submit", unavailable)
    managed_queue.dispatch_once(store)
    job = store.load(result["job_id"], "main")
    assert job["status"] == "queued"
    assert job["controller_incident"]["type"] == "OSError"
    assert json.loads(managed_queue.queue_path(store).read_text()) == [result["job_id"]]


def test_managed_agent_deployment_does_not_publish_server_limits(monkeypatch):
    from a3_research_agent.host.transport import remote_limits

    monkeypatch.setenv("A3_REMOTE_BACKEND", "process")
    monkeypatch.setenv("A3_REMOTE_MANAGED_JOBS", "1")
    limits = remote_limits()
    assert limits["managed_jobs"] is True
    for key in ("cpus", "memory", "gpu_memory_mb", "max_gpus", "data_root"):
        assert key not in limits


def test_server_assigns_available_gpu_capacity_instead_of_small_client_cap(tmp_path, monkeypatch):
    from a3_research_agent.execution.scheduling import gpu

    monkeypatch.setattr(gpu, "active_jobs", lambda store: [])

    def select(store, args, jobs, minimum, *, diagnostics):
        assert minimum == 2048
        diagnostics["devices"] = [{"uuid": "GPU-fixture", "available_mb": 12288}]
        return "GPU-fixture"

    monkeypatch.setattr(gpu, "select_shared_gpu", select)
    payload = dict(request(), gpu=True, gpu_memory_mb=2048)
    resolved = managed_queue.automatic_gpu_budget(JobStore(tmp_path), payload, options(tmp_path))
    assert resolved["gpu_memory_mb"] == 12288
    assert payload["gpu_memory_mb"] == 2048
