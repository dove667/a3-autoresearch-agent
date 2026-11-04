from remote_test_support import patch_controller

"""Fake Docker tests: never require a Docker daemon or a GPU."""

import hashlib
import json
import subprocess

import pytest

from runtime.remote import remote_jobs as runtime


@pytest.fixture
def case(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    job = store.create("owner", ["true"], False, {"artifact": "keep"}, run_id="run-one")
    job.update(container_id="a" * 64, status="completed", exit_code=0)
    store.save(job)
    info = {
        "Id": job["container_id"],
        "Name": f"/a3-job-{job['job_id']}",
        "State": {"Status": "exited", "ExitCode": 0, "FinishedAt": "fixed"},
        "Config": {
            "Image": "test",
            "Cmd": ["true"],
            "Labels": {
                "a3.root": hashlib.sha256(str(store.root).encode()).hexdigest(),
                "a3.experiment": "owner",
                "a3.job": job["job_id"],
            },
        },
    }
    calls = []
    removed = []

    def docker(args, **kwargs):
        calls.append(args)
        if args[0] == "inspect":
            if removed:
                return subprocess.CompletedProcess(args, 1, "", "No such container")
            value = info["State"] if "--format" in args else [info]
            return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
        if args[0] == "rm":
            assert "-f" not in args
            archive = store.metadata_path(job["job_id"]).parent / "terminal-archive.json"
            assert json.loads(archive.read_text())["container"] == info
            removed.append(True)
        if args[0] == "stop":
            info["State"]["Status"] = "exited"
            info["State"]["ExitCode"] = 143
        return subprocess.CompletedProcess(args, 0, "tail", "")

    patch_controller(monkeypatch, runtime, "docker", docker)
    patch_controller(
        monkeypatch,
        runtime,
        "capture_container_logs",
        lambda cid: (b"full log\xff\n", b"error log\n"),
    )
    return store, job, info, calls, removed


def reclaim(case):
    store, job, *_ = case
    with runtime.lifecycle_lock(store, job):
        return runtime.reclaim_terminal(store, job)


def test_archive_then_remove_status_and_artifacts(case):
    store, job, _, _, removed = case
    store.outputs(job).mkdir(exist_ok=True)
    (store.outputs(job) / "model").write_bytes(b"model")
    assert reclaim(case)["status"] == "removed"
    assert removed
    result = runtime.status(store, {"job_id": job["job_id"], "experiment_id": "owner"})
    assert result["status"] == "completed" and result["exit_code"] == 0
    assert result["output"] == "full log\ufffd\nerror log\n"
    assert (store.workspace(job) / "artifact").read_text() == "keep"
    assert (store.outputs(job) / "model").read_bytes() == b"model"
    assert store.run_storage_bytes("run-one") > len(b"keepmodel")


@pytest.mark.parametrize("state", ["created", "running", "paused", "restarting", "unknown"])
def test_never_remove_active_or_uncertain(case, state):
    _, _, info, calls, removed = case
    info["State"]["Status"] = state
    assert reclaim(case)["status"] == "retained"
    assert not removed and not any(x[0] == "rm" for x in calls)


@pytest.mark.parametrize("field", ["a3.root", "a3.experiment", "a3.job"])
def test_ownership_guard(case, field):
    _, _, info, _, removed = case
    info["Config"]["Labels"][field] = "other"
    assert reclaim(case)["status"] == "retained"
    assert not removed


def test_legacy_job_without_new_label(case):
    del case[2]["Config"]["Labels"]["a3.job"]
    assert reclaim(case)["status"] == "removed"


def test_archive_failure_retains_container(case, monkeypatch):
    def fail(*args):
        raise OSError("disk full")

    patch_controller(monkeypatch, runtime, "durable_json", fail)
    assert "disk full" in reclaim(case)["reason"]
    assert not case[4]


def test_rm_failure_retry(case, monkeypatch):
    original = runtime.docker

    def fail(args, **kwargs):
        if args[0] == "rm":
            raise RuntimeError("busy")
        return original(args, **kwargs)

    patch_controller(monkeypatch, runtime, "docker", fail)
    assert reclaim(case)["status"] == "retained"
    assert not case[4]
    patch_controller(monkeypatch, runtime, "docker", original)
    assert reclaim(case)["status"] == "removed"


def test_state_changes_during_archive_not_removed(case, monkeypatch):
    original = runtime.durable_json

    def changed(path, value):
        original(path, value)
        case[2]["State"]["Status"] = "running"

    patch_controller(monkeypatch, runtime, "durable_json", changed)
    assert reclaim(case)["status"] == "retained"
    assert not case[4]


def test_unfinished_watcher_not_reaped(case):
    case[1]["status"] = "running"
    assert reclaim(case)["status"] == "retained"
    assert not case[4]


def test_storage_limit_retains(case):
    case[1]["storage_limit_bytes"] = 8
    assert "storage limit" in reclaim(case)["reason"]
    assert not case[4]


def test_cancel_stops_archives_and_preserves_exit_code(case):
    store, job, info, calls, _ = case
    info["State"]["Status"] = "running"
    job["status"] = "running"
    store.save(job)
    result = runtime.cancel(store, {"job_id": job["job_id"], "experiment_id": "owner"})
    assert result["status"] == "cancelled" and result["exit_code"] == 143
    assert any(x[0] == "stop" for x in calls)
    assert not any("-f" in x for x in calls)


def test_log_capture_failure_retains(case, monkeypatch):
    def fail(cid):
        raise RuntimeError("Docker logs could not be archived")

    patch_controller(monkeypatch, runtime, "capture_container_logs", fail)
    assert reclaim(case)["status"] == "retained"
    assert not case[4]


@pytest.mark.parametrize("size,limit,success", [(100, 200, True), (1000, 200, False)])
def test_capture_stream_enforces_bound(monkeypatch, size, limit, success):
    import sys

    original = subprocess.Popen
    monkeypatch.setattr(
        runtime.subprocess,
        "Popen",
        lambda *a, **k: original(
            [sys.executable, "-c", f"import os; os.write(1, b'x'*{size}); os.write(2,b'error')"],
            **k,
        ),
    )
    patch_controller(monkeypatch, runtime, "MAX_ARCHIVE_LOG_BYTES", limit)
    if success:
        assert runtime.capture_container_logs("not-a-container") == (b"x" * size, b"error")
    else:
        with pytest.raises(RuntimeError, match="safety limit"):
            runtime.capture_container_logs("not-a-container")


def test_concurrent_status_only_removes_once(case):
    from concurrent.futures import ThreadPoolExecutor

    store, job, _, calls, _ = case
    request = {"job_id": job["job_id"], "experiment_id": "owner"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: runtime.status(store, request), range(8)))
    assert all(x["exit_code"] == 0 and x["output"] for x in results)
    assert len([x for x in calls if x[0] == "rm"]) == 1


def test_watcher_error_stops_owned_job_then_archives(case, monkeypatch):
    from argparse import Namespace

    store, job, info, calls, removed = case
    info["State"]["Status"] = "running"
    job["status"] = "running"
    store.save(job)

    def wait_error(*args, **kwargs):
        raise OSError("wait client failed")

    monkeypatch.setattr(runtime.subprocess, "run", wait_error)
    runtime.watch(Namespace(root=store.root, job_id=job["job_id"], timeout=0, lock_fd=None))
    assert removed
    assert any(x[0] == "stop" for x in calls)
    saved = store.load(job["job_id"], "owner")
    assert saved["status"] == "failed" and saved["finished_at"]
    assert "wait client failed" in saved["error"]


def test_failed_termination_reports_active_and_retries_after_exit(case, monkeypatch):
    from argparse import Namespace

    store, job, info, _, removed = case
    info["State"]["Status"] = "running"
    original = runtime.docker

    def failed_stop(args, **kwargs):
        if args[0] == "stop":
            raise RuntimeError("daemon unavailable")
        return original(args, **kwargs)

    patch_controller(monkeypatch, runtime, "docker", failed_stop)

    def wait_error(*args, **kwargs):
        raise OSError("wait client failed")

    monkeypatch.setattr(runtime.subprocess, "run", wait_error)
    runtime.watch(Namespace(root=store.root, job_id=job["job_id"], timeout=0, lock_fd=None))
    saved = store.load(job["job_id"], "owner")
    assert saved["status"] == "running" and "finished_at" not in saved
    assert "termination failed" in saved["error"]
    assert not removed
    info["State"]["Status"] = "exited"
    result = runtime.status(store, {"job_id": job["job_id"], "experiment_id": "owner"})
    assert result["status"] == "failed" and result["cleanup"]["status"] == "removed"


def test_cancel_restart_race_retains(case, monkeypatch):
    store, job, info, _, removed = case
    info["State"]["Status"] = "running"
    original = runtime.docker

    def ineffective_stop(args, **kwargs):
        if args[0] == "stop":
            return subprocess.CompletedProcess(args, 0, "", "")
        return original(args, **kwargs)

    patch_controller(monkeypatch, runtime, "docker", ineffective_stop)
    with pytest.raises(RuntimeError, match="did not stop"):
        runtime.cancel(store, {"job_id": job["job_id"], "experiment_id": "owner"})
    assert not removed


def test_cancel_created_archives_without_start_or_stop(case, monkeypatch):
    from argparse import Namespace

    store, job, info, calls, removed = case
    info["State"]["Status"] = "created"
    job["status"] = "created"
    store.save(job)
    original = runtime.docker

    def created_stop(args, **kwargs):
        if args[0] in {"start", "stop", "kill"}:
            pytest.fail("never-started cancellation must not start/stop/kill")
        return original(args, **kwargs)

    patch_controller(monkeypatch, runtime, "docker", created_stop)
    result = runtime.cancel(store, {"job_id": job["job_id"], "experiment_id": "owner"})
    assert removed and result["status"] == "cancelled"
    runtime.watch(Namespace(root=store.root, job_id=job["job_id"], timeout=0, lock_fd=None))
    assert not any(x[0] == "start" for x in calls)
    assert store.load(job["job_id"], "owner")["status"] == "cancelled"


def test_status_during_watcher_then_completion(case, monkeypatch):
    from argparse import Namespace
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    store, job, info, _, removed = case
    waiting, finish = Event(), Event()
    job["status"] = "created"
    store.save(job)
    original = runtime.docker

    def start(args, **kwargs):
        if args[0] == "start":
            info["State"]["Status"] = "running"
        return original(args, **kwargs)

    patch_controller(monkeypatch, runtime, "docker", start)

    def wait(*args, **kwargs):
        waiting.set()
        assert finish.wait(5)
        info["State"]["Status"] = "exited"
        return subprocess.CompletedProcess(args, 0, "0\n", "")

    monkeypatch.setattr(runtime.subprocess, "run", wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runtime.watch, Namespace(root=store.root, job_id=job["job_id"], timeout=0, lock_fd=None)
        )
        assert waiting.wait(5)
        result = runtime.status(store, {"job_id": job["job_id"], "experiment_id": "owner"})
        assert result["status"] == "running" and not removed
        finish.set()
        future.result(timeout=5)
    assert removed
    assert store.load(job["job_id"], "owner")["status"] == "completed"


def test_watcher_timeout_releases_lease_only_after_terminal(case, monkeypatch):
    import os
    from argparse import Namespace

    store, job, info, _, removed = case
    info["State"]["Status"] = "running"
    job["gpu_uuid"] = "GPU-test"
    store.save(job)
    lease = store.root / "locks/GPU-test.json"
    lease.write_text("{}")
    descriptor = os.open(store.root / "locks/GPU-test.lock", os.O_CREAT | os.O_RDWR, 0o600)
    original = runtime.docker

    def kill(args, **kwargs):
        if args[0] == "kill":
            info["State"].update(Status="exited", ExitCode=137)
        return original(args, **kwargs)

    patch_controller(monkeypatch, runtime, "docker", kill)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker wait", 1)

    monkeypatch.setattr(runtime.subprocess, "run", timeout)
    runtime.watch(Namespace(root=store.root, job_id=job["job_id"], timeout=1, lock_fd=descriptor))
    assert removed and not lease.exists()
    with pytest.raises(OSError):
        os.fstat(descriptor)
    result = runtime.status(store, {"job_id": job["job_id"], "experiment_id": "owner"})
    assert result["status"] == "failed" and result["exit_code"] == 137


def test_failed_termination_retains_gpu_lease_record(case, monkeypatch):
    import os
    from argparse import Namespace

    store, job, info, _, removed = case
    info["State"]["Status"] = "running"
    job["gpu_uuid"] = "GPU-test"
    store.save(job)
    lease = store.root / "locks/GPU-test.json"
    lease.write_text(json.dumps({"container_id": job["container_id"]}))
    descriptor = os.open(store.root / "locks/GPU-test.lock", os.O_CREAT | os.O_RDWR, 0o600)
    original = runtime.docker

    def failed_stop(args, **kwargs):
        if args[0] == "stop":
            raise RuntimeError("stop unavailable")
        return original(args, **kwargs)

    patch_controller(monkeypatch, runtime, "docker", failed_stop)

    def wait_error(*args, **kwargs):
        raise OSError("wait client failed")

    monkeypatch.setattr(runtime.subprocess, "run", wait_error)
    runtime.watch(Namespace(root=store.root, job_id=job["job_id"], timeout=0, lock_fd=descriptor))
    assert lease.exists() and not removed
    assert store.load(job["job_id"], "owner")["status"] == "running"


def test_created_cancel_archive_failure_watcher_preserves_retry(case, monkeypatch):
    from argparse import Namespace

    store, job, info, _, removed = case
    info["State"]["Status"] = "created"
    job["status"] = "created"
    store.save(job)
    original = runtime.durable_json

    def disk_full(*args, **kwargs):
        raise OSError("disk full")

    patch_controller(monkeypatch, runtime, "durable_json", disk_full)
    result = runtime.cancel(store, {"job_id": job["job_id"], "experiment_id": "owner"})
    assert result["status"] == "cancelled" and not removed
    runtime.watch(Namespace(root=store.root, job_id=job["job_id"], timeout=0, lock_fd=None))
    assert store.load(job["job_id"], "owner")["status"] == "cancelled"
    patch_controller(monkeypatch, runtime, "durable_json", original)
    result = runtime.status(store, {"job_id": job["job_id"], "experiment_id": "owner"})
    assert result["status"] == "cancelled" and removed


def test_uncertain_docker_state_returns_cleanup_reason_not_delete(case, monkeypatch):
    store, job, _, _, removed = case

    def unavailable(*args, **kwargs):
        raise RuntimeError("daemon unavailable")

    patch_controller(monkeypatch, runtime, "docker", unavailable)
    result = runtime.status(store, {"job_id": job["job_id"], "experiment_id": "owner"})
    assert result["cleanup"]["status"] == "retained"
    assert "daemon unavailable" in result["cleanup"]["reason"]
    assert not removed
