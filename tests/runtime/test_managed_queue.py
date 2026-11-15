"""Server admission persists jobs while resources are busy, without model retries."""

import json
import sys
from argparse import Namespace
from pathlib import Path

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
        managed_gpu_memory_mb=8192,
        gpu_capacity_mb=20000,
        gpu_headroom_mb=2048,
        gpu_ids="",
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


def test_managed_reservations_keep_explicit_peaks_without_probing_resources(monkeypatch):
    from a3_research_agent.execution.scheduling import gpu

    def forbidden(*args, **kwargs):
        raise AssertionError("Reservation normalization must not sample the server")

    monkeypatch.setattr(gpu, "active_jobs", forbidden)
    monkeypatch.setattr(gpu, "select_shared_gpu", forbidden)
    payload = dict(request(), gpu=True, gpu_memory_mb=2048)
    resolved = managed_queue.managed_gpu_request(payload, options(Path("unused")))
    assert resolved["gpu_memory_mb"] == 2048
    assert payload["gpu_memory_mb"] == 2048


def test_managed_default_is_bounded_by_configured_device_capacity(tmp_path):
    args = options(tmp_path)
    payload = dict(request(), gpu=True)
    assert managed_queue.managed_gpu_request(payload, args)["gpu_memory_mb"] == 8192
    args.managed_gpu_memory_mb = 4096
    assert managed_queue.managed_gpu_request(payload, args)["gpu_memory_mb"] == 4096
    args.gpu_capacity_mb = 2048
    assert managed_queue.managed_gpu_request(payload, args)["gpu_memory_mb"] == 2048


def test_impossible_configured_reservation_is_rejected_before_upload(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    monkeypatch.setattr(processes, "validate_data_paths", lambda *args: None)
    result = managed_queue.enqueue(
        store, dict(request(), gpu=True, gpu_memory_mb=20001), options(tmp_path)
    )
    assert result["status"] == "resource_unavailable"
    assert result["job_id"] is None
    assert not list((tmp_path / "jobs").iterdir())


def gpu_server(monkeypatch):
    from a3_research_agent.execution.scheduling import gpu

    probes = []

    def devices():
        probes.append("devices")
        return [
            {"index": "0", "uuid": "GPU-fixture", "total_mb": 24576, "used_mb": 0, "utilization": 0}
        ]

    monkeypatch.setattr(gpu, "gpu_devices", devices)
    monkeypatch.setattr(gpu, "gpu_process_memory", dict)
    monkeypatch.setattr(processes, "validate_data_paths", lambda *args: None)
    monkeypatch.setattr(processes, "process_ids", lambda job: [])
    monkeypatch.setattr(host_resources, "sample", lambda root: {})
    monkeypatch.setattr(host_resources, "admission", lambda *args: None)
    monkeypatch.setattr(managed_queue, "ensure_dispatcher", lambda store: None)
    launched = []
    monkeypatch.setattr(submission.subprocess, "Popen", lambda *a, **kw: launched.append(a))
    return probes, launched


def test_two_managed_small_jobs_share_device_with_one_probe_each(tmp_path, monkeypatch):
    probes, launched = gpu_server(monkeypatch)
    store = JobStore(tmp_path)
    identifiers = [
        managed_queue.enqueue(
            store, dict(request(), gpu=True, gpu_memory_mb=2048), options(tmp_path)
        )["job_id"]
        for _ in range(2)
    ]
    assert managed_queue.dispatch_once(store) == 2
    jobs = [store.load(identifier, "main") for identifier in identifiers]
    assert [job["gpu_memory_mb"] for job in jobs] == [2048, 2048]
    assert [job["gpu_uuid"] for job in jobs] == ["GPU-fixture", "GPU-fixture"]
    assert len(launched) == 2
    assert probes == ["devices", "devices"]


def test_permanent_physical_shortage_finishes_queued_job_without_launch(tmp_path, monkeypatch):
    _, launched = gpu_server(monkeypatch)
    store = JobStore(tmp_path)
    args = options(tmp_path)
    args.gpu_capacity_mb = 0
    result = managed_queue.enqueue(store, dict(request(), gpu=True, gpu_memory_mb=23000), args)
    assert result["status"] == "queued"
    assert managed_queue.dispatch_once(store) == 0
    job = store.load(result["job_id"], "main")
    assert job["status"] == "failed"
    assert "capacity" in job["error"]
    assert not launched


def test_temporary_gpu_pressure_keeps_id_until_memory_is_available(tmp_path, monkeypatch):
    from a3_research_agent.execution.scheduling import gpu

    _, launched = gpu_server(monkeypatch)
    occupied = {
        "index": "0",
        "uuid": "GPU-fixture",
        "total_mb": 24576,
        "used_mb": 12000,
        "utilization": 95,
    }
    monkeypatch.setattr(gpu, "gpu_devices", lambda: [occupied])
    store = JobStore(tmp_path)
    result = managed_queue.enqueue(
        store, dict(request(), gpu=True, gpu_memory_mb=16000), options(tmp_path)
    )
    identifier = result["job_id"]
    assert managed_queue.dispatch_once(store) == 1
    assert store.load(identifier, "main")["status"] == "queued"
    assert not launched
    occupied["used_mb"] = 0
    assert managed_queue.dispatch_once(store) == 1
    job = store.load(identifier, "main")
    assert job["status"] == "created"
    assert job["gpu_memory_mb"] == 16000
    assert len(launched) == 1


def test_host_forwards_managed_default_budget(monkeypatch):
    import shlex
    import subprocess

    from a3_research_agent.host import transport

    monkeypatch.setenv("A3_REMOTE_HOST", "configured-server")
    monkeypatch.setenv("A3_REMOTE_CONTROLLER", "controller.py")
    monkeypatch.setenv("A3_REMOTE_RUN_ROOT", "/data/project/runtime")
    monkeypatch.setenv("A3_REMOTE_MANAGED_GPU_MEMORY_MB", "6144")
    commands = []

    def ssh(arguments, **kwargs):
        commands.append(shlex.split(arguments[-1]))
        return subprocess.CompletedProcess(arguments, 0, "{}", "")

    monkeypatch.setattr(transport.subprocess, "run", ssh)
    assert transport.remote_call("jobs-status", {"job_ids": []}) == {}
    command = commands[0]
    assert command[command.index("--managed-gpu-memory-mb") + 1] == "6144"
