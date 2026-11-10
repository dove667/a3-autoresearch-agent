"""Trusted process jobs coexist with legacy Docker jobs without restarting them."""

import json
import os
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from a3_research_agent.execution.controller import processes
from a3_research_agent.execution.controller.store import JobStore
from a3_research_agent.execution.scheduling import host_resources


def options(root):
    return Namespace(
        root=root,
        backend="process",
        data_root=root,
        tmp_root=root / "temp",
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
    )


def test_native_environment_uses_data_paths_and_excludes_credentials(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = store.create("a", ["python", "run.py"], False, {}, run_id="run-a")
    monkeypatch.setattr(processes, "validate_data_paths", lambda *args: None)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    processes.prepare(store, job, options(tmp_path))
    env = processes.environment(job)
    assert "AWS_SECRET_ACCESS_KEY" not in env
    for name in ("HOME", "TMPDIR", "HF_HOME", "CUDA_CACHE_PATH", "XDG_CACHE_HOME"):
        assert Path(env[name]).is_relative_to(tmp_path)
    assert env["A3_WORKSPACE"] == str(store.workspace(job))
    assert env["A3_OUTPUTS"] == str(store.outputs(job))
    assert env["CUDA_VISIBLE_DEVICES"] == ""
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert job["backend"] == "process"
    assert job["resource_limits"]["cpus"] is None
    assert job["resource_limits"]["memory"] is None
    context = json.loads((store.workspace(job) / "a3-job-context.json").read_text())
    assert context["version"] == "0.1.0"
    assert context["paths"]["workspace"] == str(store.workspace(job))


def test_system_disk_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="data disk"):
        processes.validate_data_paths(tmp_path, [tmp_path])


def test_paths_outside_data_root_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(processes, "different_device_from_system", lambda path: True)
    (tmp_path / "data").mkdir()
    with pytest.raises(ValueError, match="data root"):
        processes.validate_data_paths(tmp_path / "data", [tmp_path / "outside"])


def test_process_identity_checks_start_time_and_boot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        processes,
        "read_process",
        lambda pid: {"pid": pid, "start_ticks": 99, "state": "S", "pgrp": pid},
    )
    monkeypatch.setattr(processes, "boot_id", lambda: "boot-a")
    job = {"process": {"pid": 123, "start_ticks": 99, "boot_id": "boot-a"}}
    assert processes.is_running(job)
    job["process"]["start_ticks"] = 98
    assert not processes.is_running(job)
    job["process"].update(start_ticks=99, boot_id="old-boot")
    assert not processes.is_running(job)


def test_cancel_never_signals_reused_pid(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = store.create("a", ["true"], False, {}, run_id="run-a")
    job.update(
        backend="process",
        status="running",
        process={"pid": 123, "start_ticks": 1, "boot_id": "boot"},
    )
    store.save(job)
    monkeypatch.setattr(
        processes,
        "read_process",
        lambda pid: {"pid": pid, "start_ticks": 2, "state": "S", "pgrp": pid},
    )
    monkeypatch.setattr(processes, "boot_id", lambda: "boot")
    monkeypatch.setattr(os, "killpg", lambda *args: pytest.fail("must not signal reused PID"))
    result = processes.cancel(store, {"experiment_id": "a", "job_id": job["job_id"]})
    assert result["status"] == "cancelled"


def test_host_admission_accounts_for_pending_memory_and_disk():
    sample = {"cpus": 16, "load": 2, "memory_available_mb": 100, "disk_free_bytes": 1000}
    args = options(Path("unused"))
    jobs = [{"backend": "process", "host_memory_mb": 80, "observed_memory_mb": 0}]
    assert host_resources.admission(sample, args, jobs)["reason"] == "host_memory_pressure"
    assert (
        host_resources.admission(dict(sample, disk_free_bytes=1), args, [])["reason"]
        == "data_disk_headroom"
    )
    assert (
        host_resources.admission(dict(sample, load=16), args, [])["reason"] == "host_cpu_pressure"
    )
    assert host_resources.admission(sample, args, []) is None


def test_missing_process_watcher_is_not_reported_as_still_running(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = store.create("a", ["true"], False, {}, run_id="run-a")
    job.update(
        backend="process",
        status="running",
        process={"pid": 123, "start_ticks": 1, "boot_id": "boot"},
    )
    store.save(job)
    monkeypatch.setattr(processes, "read_process", lambda pid: None)
    monkeypatch.setattr(processes, "process_ids", lambda job: [])
    monkeypatch.setattr(processes, "boot_id", lambda: "boot")
    result = processes.status(store, {"experiment_id": "a", "job_id": job["job_id"]})
    assert result["status"] == "failed"
    assert "exit status" in result["error"]


def test_large_legacy_inventory_does_not_inspect_absent_containers(tmp_path, monkeypatch):
    import subprocess

    from a3_research_agent.execution.scheduling import gpu

    store = JobStore(tmp_path)
    records = []
    for index in range(34):
        job = store.create("a", ["true"], False, {})
        job.update(status="completed", container_id=f"{index:064x}")
        store.save(job)
        records.append(job)
    live = records[-1]
    monkeypatch.setattr(
        gpu,
        "docker",
        lambda *a, **kw: subprocess.CompletedProcess([], 0, live["container_id"] + "\n", ""),
    )
    seen = []
    monkeypatch.setattr(
        gpu,
        "container_states",
        lambda ids: seen.extend(ids) or {cid: {"Status": "running"} for cid in ids},
    )
    assert gpu.active_jobs(store) == [live]
    assert seen == [live["container_id"]]
    assert len(list((store.root / "jobs").glob("*/container-absent.json"))) == 33


def test_native_episode_monitor_uses_host_available_memory(tmp_path, monkeypatch):
    from a3_research_agent.execution.episode_executor import ResourceMonitor

    monkeypatch.setenv("A3_JOB_RESOURCE_BACKEND", "process")
    monkeypatch.setenv("A3_HOST_MEMORY_RESERVE_MB", "1")
    (tmp_path / "meminfo").write_text("MemTotal: 10000 kB\nMemAvailable: 8000 kB\n")
    (tmp_path / "stat").write_text("cpu 10 0 0 90 0 0 0 0\n")
    monitor = ResourceMonitor(tmp_path)
    first = monitor.sample()
    assert first["memory_bytes"] == 2000 * 1024
    assert first["memory_limit"] == 10000 * 1024 - 1024**2
    (tmp_path / "stat").write_text("cpu 20 0 0 180 0 0 0 0\n")
    assert monitor.sample()["cpu_cores"] == pytest.approx(monitor.cpus * 0.1)


def test_failed_native_attempt_does_not_reserve_gpu_while_retry_is_decided(tmp_path, monkeypatch):
    from a3_research_agent.execution.scheduling import gpu

    store = JobStore(tmp_path)
    job = store.create("a", ["true"], True, {}, run_id="run-a")
    job.update(backend="process", status="failed", retry_pending=True)
    store.save(job)
    monkeypatch.setattr(processes, "is_running", lambda job: False)
    monkeypatch.setattr(processes, "process_ids", lambda job: pytest.fail("no live attempt"))
    assert gpu.active_jobs(store) == []


def test_process_deployment_advertises_real_paths_instead_of_container_aliases(monkeypatch):
    from a3_research_agent.host import transport

    monkeypatch.setenv("A3_REMOTE_BACKEND", "process")
    monkeypatch.setenv("A3_REMOTE_A3_ROOT", "/data/project/a3")
    hints = transport.remote_path_hints()
    assert hints["remote_a3"] == "/data/project/a3"
    assert hints["remote_workspace"] == "paths.workspace / A3_WORKSPACE"
    assert "/outputs" not in hints["remote_outputs"]


def test_process_limits_ignore_legacy_container_hard_caps(monkeypatch):
    from a3_research_agent.host.transport import remote_limits

    monkeypatch.setenv("A3_REMOTE_MANAGED_JOBS", "0")
    monkeypatch.setenv("A3_REMOTE_BACKEND", "process")
    monkeypatch.setenv("A3_REMOTE_CPUS", "16")
    monkeypatch.setenv("A3_REMOTE_MEMORY", "64g")
    limits = remote_limits()
    assert limits["cpus"] is None
    assert limits["memory"] is None
    assert limits["pids_limit"] is None
