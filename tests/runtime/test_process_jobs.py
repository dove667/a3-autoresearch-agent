"""Run and observe remote jobs using native Linux processes."""

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


def test_scheduler_and_job_listing_ignore_unsupported_job_records(tmp_path):
    from a3_research_agent.execution.scheduling import gpu

    store = JobStore(tmp_path)
    old = store.create("a", ["true"], False, {}, run_id="run-a")
    old.pop("backend")
    old["status"] = "running"
    store.save(old)
    current = store.create("a", ["true"], False, {}, run_id="run-a")

    assert [job["job_id"] for job in gpu.active_jobs(store)] == [current["job_id"]]
    listing = store.list_jobs({"experiment_id": "a", "run_id": "run-a"})
    assert [job["job_id"] for job in listing["jobs"]] == [current["job_id"]]
    with pytest.raises(PermissionError, match="job is not accessible"):
        store.load(old["job_id"], "a")


def test_native_episode_monitor_uses_host_available_memory(tmp_path, monkeypatch):
    from a3_research_agent.execution.episode_executor import ResourceMonitor

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
    monkeypatch.setattr(gpu, "is_running", lambda job: False)
    monkeypatch.setattr(gpu, "process_ids", lambda job: pytest.fail("no live attempt"))
    assert gpu.active_jobs(store) == []


def test_process_deployment_advertises_real_paths(monkeypatch):
    from a3_research_agent.host import remote_client as transport

    monkeypatch.setenv("A3_REMOTE_A3_ROOT", "/data/project/a3")
    monkeypatch.setenv("A3_REMOTE_DATASETS_ROOT", "/data/project/DATASETS")
    monkeypatch.setenv("A3_REMOTE_MODELS_ROOT", "/data/project/MODELS")
    hints = transport.remote_path_hints()
    assert hints["remote_a3"] == "/data/project/a3"
    assert hints["remote_datasets"] == "/data/project/DATASETS"
    assert hints["remote_models"] == "/data/project/MODELS"
    assert hints["remote_workspace"] == "paths.workspace / A3_WORKSPACE"
    assert "/outputs" not in hints["remote_outputs"]


def test_controller_health_uses_configured_process_environment(tmp_path, monkeypatch, capsys):
    import io
    from types import SimpleNamespace

    from a3_research_agent.execution.controller import cli, processes
    from a3_research_agent.execution.scheduling import host_resources

    monkeypatch.setattr(processes, "different_device_from_system", lambda path: True)
    monkeypatch.setattr(host_resources, "sample", lambda path: {"disk_free_bytes": 100})
    python = tmp_path / "python"
    python.touch()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "controller",
            "--root",
            str(tmp_path / "jobs"),
            "--data-root",
            str(tmp_path),
            "--job-python",
            str(python),
            "health",
        ],
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}")))
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok"
    assert result["backend"] == "process"
    assert result["python"] == str(python)


def test_native_watcher_runs_cpu_job_and_retains_output(tmp_path, monkeypatch):
    import base64

    from a3_research_agent.execution.controller import artifacts, lifecycle

    store = JobStore(tmp_path / "runtime")
    job = store.create(
        "owner",
        ["python", "runner.py"],
        False,
        {
            "runner.py": (
                "from pathlib import Path\n"
                "Path('result.txt').write_text('evaluation result')\n"
                "print('native watcher completed')\n"
            )
        },
        run_id="run-a",
    )
    monkeypatch.setattr(processes, "validate_data_paths", lambda *args: None)
    monkeypatch.setattr(host_resources, "sample", lambda path: {"disk_free_bytes": 1000})
    if sys.platform != "linux":
        monkeypatch.setattr(processes, "read_process", lambda pid: {"start_ticks": pid})
        monkeypatch.setattr(processes, "boot_id", lambda: "test-boot")
        monkeypatch.setattr(processes, "process_ids", lambda job: [])
    processes.prepare(store, job, options(tmp_path))

    lifecycle.watch(Namespace(root=store.root, job_id=job["job_id"], timeout=5, lock_fd=None))

    request = {"experiment_id": "owner", "job_id": job["job_id"]}
    result = processes.status(store, request)
    assert (result["status"], result["exit_code"]) == ("completed", 0)
    artifact = artifacts.read_artifact(store, {**request, "path": "result.txt"})
    assert base64.b64decode(artifact["data"]) == b"evaluation result"
    path = store.metadata_path(job["job_id"]).with_name("terminal-archive.json")
    archive = json.loads(path.read_text())
    assert base64.b64decode(archive["stdout_base64"]) == b"native watcher completed\n"
