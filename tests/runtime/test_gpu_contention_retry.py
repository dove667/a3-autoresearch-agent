"""Retry restart-safe jobs only when OOM evidence shows competing GPU memory."""

import base64
import json
from argparse import Namespace
from unittest.mock import Mock

import pytest

from a3_research_agent.execution.controller import files, store
from a3_research_agent.host.gateway.schemas import JobRequest

OOM = (
    'File "modeling_policy.py", line 10, in from_pretrained\n'
    "  model = cls(config)\n"
    'File "modeling_policy.py", line 20, in __init__\n'
    "  self.model.to(config.device)\n"
    "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 64.00 MiB. "
    "GPU 0 has a total capacity of 23.52 GiB of which 28.31 MiB is free. "
    "Including non-PyTorch memory, this process has 5.58 GiB memory in use."
)


def retry_module():
    from a3_research_agent.execution.controller import retry

    return retry


def make_job(tmp_path):
    database = store.JobStore(tmp_path)
    job = database.create("owner", ["python", "train.py"], True, {}, run_id="run-test")
    job.update(
        status="failed",
        container_id="old",
        gpu_uuid="GPU-old",
        gpu_shared=True,
        gpu_memory_mb=20480,
        retry_on_gpu_contention=True,
        exit_code=1,
        controller_options={"max_jobs": 4, "timeout": 100, "image": "fixed"},
    )
    database.save(job)
    directory = database.metadata_path(job["job_id"]).parent
    archive = {"stderr_base64": base64.b64encode(OOM.encode()).decode(), "stdout_base64": ""}
    (directory / "terminal-archive.json").write_text(json.dumps(archive))
    (directory / "cleanup.json").write_text(json.dumps({"status": "removed"}))
    return database, job


def test_gpu_contention_retry_is_enabled_by_default_and_can_be_disabled():
    assert JobRequest(command=["true"]).retry_on_gpu_contention is True
    assert JobRequest(
        command=["true"], gpu=True, retry_on_gpu_contention=True
    ).retry_on_gpu_contention
    files.validate_submit(
        {
            "experiment_id": "owner",
            "command": ["true"],
            "gpu": True,
            "retry_on_gpu_contention": True,
        }
    )
    for value in ("true", 1, None):
        with pytest.raises((ValueError, TypeError)):
            files.validate_submit(
                {
                    "experiment_id": "owner",
                    "command": ["true"],
                    "gpu": True,
                    "retry_on_gpu_contention": value,
                }
            )


def test_oom_classifier_requires_competition_not_just_oom(tmp_path):
    retry = retry_module()
    _, job = make_job(tmp_path)
    assert retry.gpu_contention_evidence(job, OOM)["other_used_mb"] > 17000
    assert retry.gpu_contention_evidence(job, OOM.replace("5.58 GiB", "23.40 GiB")) is None
    assert retry.gpu_contention_evidence(job, "CUDA out of memory") is None
    assert retry.gpu_contention_evidence(job, OOM.replace("28.31 MiB", "22.00 GiB")) is None
    assert retry.gpu_contention_evidence(job, "ordinary script error") is None
    assert retry.gpu_contention_evidence(job, OOM[OOM.index("torch.OutOfMemoryError") :]) is None


def test_retry_reselects_gpu_keeps_identity_and_archives_attempt(tmp_path, monkeypatch):
    retry = retry_module()
    database, job = make_job(tmp_path)
    monkeypatch.setattr(retry, "active_jobs", lambda *_: [])
    monkeypatch.setattr(retry, "select_shared_gpu", lambda *a, **k: "GPU-new")
    monkeypatch.setattr(retry, "create_container", lambda *a: "new")
    assert retry.prepare_retry(database, job) is True
    saved = database.load(job["job_id"], "owner")
    assert saved["gpu_uuid"] == "GPU-new"
    assert saved["container_id"] == "new"
    assert saved["status"] == "created"
    assert saved["retry_count"] == 1
    assert saved["command"] == job["command"]
    directory = database.metadata_path(job["job_id"]).parent
    assert (directory / "attempts/1/terminal-archive.json").exists()
    assert not (directory / "cleanup.json").exists()
    assert database.run_storage_bytes("run-test") > 0


@pytest.mark.parametrize("block", ["busy", "limit", "cancel", "retained", "unsafe", "self_oom"])
def test_retry_stops_without_new_container_when_not_safe(tmp_path, monkeypatch, block):
    retry = retry_module()
    database, job = make_job(tmp_path)
    directory = database.metadata_path(job["job_id"]).parent
    if block == "limit":
        job["retry_count"] = 2
    if block == "cancel":
        (directory / "cancel_requested").touch()
    if block == "retained":
        (directory / "cleanup.json").write_text('{"status":"retained"}')
    if block == "unsafe":
        job["retry_on_gpu_contention"] = False
    if block == "self_oom":
        archive = json.loads((directory / "terminal-archive.json").read_text())
        archive["stderr_base64"] = base64.b64encode(
            OOM.replace("5.58 GiB", "23.40 GiB").encode()
        ).decode()
        (directory / "terminal-archive.json").write_text(json.dumps(archive))
    database.save(job)
    create = Mock(side_effect=AssertionError("must not create a container"))
    monkeypatch.setattr(retry, "create_container", create)
    monkeypatch.setattr(retry, "active_jobs", lambda *_: [])
    monkeypatch.setattr(retry, "select_shared_gpu", lambda *a, **k: None)
    assert retry.prepare_retry(database, job) is False
    assert not create.called
    saved = database.load(job["job_id"], "owner")
    if block == "busy":
        assert saved["retry"]["status"] == "resource_busy"
        assert saved["retry"]["reason"] == "no_eligible_gpu"


def test_watcher_runs_next_attempt_without_another_submit(tmp_path, monkeypatch):
    retry = retry_module()
    database, job = make_job(tmp_path)
    calls = []

    def attempt(args):
        calls.append(args.job_id)
        current = database.load(job["job_id"], "owner")
        current["status"] = "failed" if len(calls) == 1 else "completed"
        database.save(current)

    def prepare(db, current):
        return len(calls) == 1

    monkeypatch.setattr(retry, "prepare_retry", prepare)
    retry.watch_with_retries(
        Namespace(root=database.root, job_id=job["job_id"], timeout=0, lock_fd=None), attempt
    )
    assert calls == [job["job_id"], job["job_id"]]
    assert database.load(job["job_id"], "owner")["status"] == "completed"


def test_retry_does_not_bypass_run_storage_limit(tmp_path, monkeypatch):
    retry = retry_module()
    database, job = make_job(tmp_path)
    job["storage_limit_bytes"] = database.run_storage_bytes("run-test") + 1
    database.save(job)
    monkeypatch.setattr(retry, "active_jobs", lambda *_: [])
    monkeypatch.setattr(retry, "select_shared_gpu", lambda *a, **k: "GPU-new")
    monkeypatch.setattr(retry, "create_container", Mock(side_effect=AssertionError("storage full")))
    assert not retry.prepare_retry(database, job)
    assert database.load(job["job_id"], "owner")["retry"]["status"] == "storage_limit"


def test_public_status_hides_failed_attempt_until_retry_decided(tmp_path):
    database, job = make_job(tmp_path)
    job["retry_pending"] = True
    database.save(job)
    assert files.public_job_status(job) == "restarting"
    listing = database.list_jobs({"experiment_id": "owner", "run_id": "run-test"})
    assert listing["jobs"][0]["status"] == "restarting"


def test_restart_does_not_reset_total_timeout(tmp_path, monkeypatch):
    retry = retry_module()
    database, job = make_job(tmp_path)
    clocks = iter([0, 0, 2, 3, 5])
    monkeypatch.setattr(retry.time, "monotonic", lambda: next(clocks))
    seen = []
    monkeypatch.setattr(retry, "prepare_retry", lambda *_: len(seen) == 1)
    retry.watch_with_retries(
        Namespace(root=database.root, job_id=job["job_id"], timeout=10),
        lambda args: seen.append(args.timeout),
    )
    assert seen == [10, 7]


def test_removed_container_detection_accepts_docker_desktop_error_case(monkeypatch):
    import subprocess

    from a3_research_agent.execution.controller import docker

    monkeypatch.setattr(
        docker,
        "docker",
        lambda *a, **k: subprocess.CompletedProcess([], 1, "", "error: no such object: old"),
    )
    assert docker.container_state("old")["Status"] == "removed"


def test_cancel_reloads_container_after_waiting_for_retry_lock(tmp_path, monkeypatch):
    import subprocess
    from contextlib import contextmanager

    from a3_research_agent.execution.controller import lifecycle

    database, job = make_job(tmp_path)
    job.update(status="running", retry_pending=True)
    database.save(job)

    @contextmanager
    def swapped_container(*args):
        current = database.load(job["job_id"], "owner")
        current["container_id"] = "new-container"
        database.save(current)
        yield

    states = {"old": {"Status": "running"}, "new-container": {"Status": "running"}}
    stopped = []

    def docker(command, **kwargs):
        if command[0] == "stop":
            stopped.append(command[-1])
            states[command[-1]] = {"Status": "exited", "ExitCode": 143}
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(lifecycle, "lifecycle_lock", swapped_container)
    monkeypatch.setattr(lifecycle, "owned_container", lambda *args: {})
    monkeypatch.setattr(lifecycle, "container_state", lambda identifier: states[identifier])
    monkeypatch.setattr(lifecycle, "docker", docker)
    monkeypatch.setattr(lifecycle, "reclaim_terminal", lambda *args: {"status": "retained"})
    lifecycle.cancel(database, {"experiment_id": "owner", "job_id": job["job_id"]})
    assert stopped == ["new-container"]
