import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from remote_test_support import patch_controller

from a3_research_agent.execution.controller import artifacts as job_artifacts
from a3_research_agent.execution.controller import cli as controller_cli
from a3_research_agent.execution.controller import files as job_files
from a3_research_agent.execution.controller import store as job_store
from a3_research_agent.execution.controller import submission as job_submission
from a3_research_agent.execution.scheduling import gpu as job_gpu

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "src/a3_research_agent/execution/controller/__main__.py"
)


def controller_arguments(tmp_path, operation):
    return [
        "controller",
        "--root",
        str(tmp_path / "jobs"),
        "--data-root",
        str(tmp_path),
        "--job-python",
        sys.executable,
        operation,
    ]


def test_gpu_snapshot_returns_aggregate_device_metrics(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", controller_arguments(tmp_path, "gpu-snapshot"))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}")))
    monkeypatch.setattr(
        job_gpu,
        "gpu_devices",
        lambda: [
            {
                "index": "2",
                "uuid": "internal-device-id",
                "total_mb": 24564,
                "used_mb": 5000,
                "utilization": 14,
            }
        ],
    )
    controller_cli.main()
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["observed_at"] > 0
    assert snapshot["gpus"] == [
        {"index": "2", "total_mb": 24564, "used_mb": 5000, "free_mb": 19564, "utilization": 14}
    ]


def test_workspace_files_reject_escape_and_symlinks(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    for path in ("../outside/leak", "/absolute", "linked/leak"):
        with pytest.raises(ValueError):
            job_files.write_files(workspace, {path: "blocked"})
    job_files.write_files(workspace, {"nested/input.txt": "hello"})
    assert (workspace / "nested/input.txt").read_text() == "hello"
    assert list(outside.iterdir()) == []


def test_job_ownership_is_checked_before_status_or_artifact_access(tmp_path):
    store = job_store.JobStore(tmp_path)
    job = store.create("experiment-a", ["python", "-c", "print(1)"], False, {})
    assert store.load(job["job_id"], "experiment-a")["experiment_id"] == "experiment-a"
    for owner in ("experiment-b", "", None):
        with pytest.raises(PermissionError):
            store.load(job["job_id"], owner)
    with pytest.raises(PermissionError):
        store.load("missing", "experiment-a")


def test_gpu_lease_excludes_another_controller_process(tmp_path):
    lease = job_gpu.GpuLease.try_acquire(tmp_path, "GPU-test")
    assert lease is not None
    code = (
        "import sys; from pathlib import Path; "
        "from a3_research_agent.execution.scheduling.gpu import GpuLease; "
        'lease = GpuLease.try_acquire(Path(sys.argv[1]), "GPU-test"); '
        'print("busy" if lease is None else "acquired")'
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)],
            cwd=MODULE_PATH.parents[4],
            env={**__import__("os").environ, "PYTHONPATH": str(MODULE_PATH.parents[3])},
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "busy"
    finally:
        lease.close()
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=MODULE_PATH.parents[4],
        env={**__import__("os").environ, "PYTHONPATH": str(MODULE_PATH.parents[3])},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "acquired"


def test_submit_rejects_host_options_and_malformed_commands():
    for request in (
        {"experiment_id": "a", "command": ["true"], "gpu": False, "mounts": ["/"]},
        {"experiment_id": "a", "command": "true", "gpu": False},
        {"experiment_id": "a", "command": [], "gpu": False},
        {"experiment_id": "../a", "command": ["true"], "gpu": False},
        {"experiment_id": "a", "command": ["true"], "gpu": "false"},
    ):
        with pytest.raises((ValueError, TypeError)):
            job_files.validate_submit(request)


def test_artifact_reads_owner_workspace_and_rejects_symlinks(tmp_path):
    store = job_store.JobStore(tmp_path)
    job = store.create("a", ["true"], False, {"result.json": '{"ok": true}'})
    workspace = store.workspace(job)
    outside = tmp_path / "private.txt"
    outside.write_text("secret")
    (workspace / "linked.txt").symlink_to(outside)
    result = job_artifacts.read_artifact(
        store, {"experiment_id": "a", "job_id": job["job_id"], "path": "result.json"}
    )
    assert result["encoding"] == "base64"
    assert result["data"] == "eyJvayI6IHRydWV9"
    for path in ("../private.txt", "linked.txt", "/private.txt"):
        with pytest.raises(ValueError):
            job_artifacts.read_artifact(
                store, {"experiment_id": "a", "job_id": job["job_id"], "path": path}
            )
    with pytest.raises(PermissionError):
        job_artifacts.read_artifact(
            store, {"experiment_id": "b", "job_id": job["job_id"], "path": "result.json"}
        )


@pytest.mark.parametrize("operation", ["status", "cancel", "artifact"])
def test_unknown_job_cli_returns_json_error_without_revealing_ownership(
    tmp_path, operation, monkeypatch, capsys
):
    from a3_research_agent.execution.controller import processes

    monkeypatch.setattr(processes, "different_device_from_system", lambda path: True)
    monkeypatch.setattr(sys, "argv", controller_arguments(tmp_path, operation))
    request = json.dumps({"experiment_id": "a", "job_id": "missing", "path": "result.json"})
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(request.encode())))
    controller_cli.main()
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": "PermissionError",
        "message": "job is not accessible",
    }


def test_run_storage_combines_owners_and_ignores_other_runs(tmp_path):
    store = job_store.JobStore(tmp_path)
    first = store.create("a", ["true"], False, {"data": "123"}, run_id="run-one")
    second = store.create("b", ["true"], False, {"data": "4567"}, run_id="run-one")
    store.create("a", ["true"], False, {"data": "other"}, run_id="run-two")
    assert store.run_storage_bytes("run-one") == 7
    assert store.workspace(first) != store.workspace(second)
    outside = tmp_path / "outside"
    outside.write_text("not part of quota")
    (store.workspace(first) / "link").symlink_to(outside)
    assert store.run_storage_bytes("run-one") == 7


def test_storage_limit_blocks_new_job_before_gpu_allocation(tmp_path, monkeypatch):
    from argparse import Namespace

    store = job_store.JobStore(tmp_path)
    store.create("old-owner", ["true"], False, {"data": "1234567"}, run_id="run-one")
    patch_controller(monkeypatch, "active_jobs", lambda *a: pytest.fail("scheduler reached"))
    result = job_submission.submit(
        store,
        {
            "experiment_id": "new-owner",
            "run_id": "run-one",
            "command": ["true"],
            "gpu": True,
            "files": {"data": "12"},
        },
        Namespace(run_storage_bytes=8, max_jobs=4),
    )
    assert result["status"] == "storage_limit"
    assert result["used_bytes"] == 7


def test_run_storage_includes_latest_host_report(tmp_path):
    store = job_store.JobStore(tmp_path)
    store.create("a", ["true"], False, {"data": "123"}, run_id="run-one")
    result = store.update_host_storage({"run_id": "run-one", "host_storage_bytes": 11})
    assert result["used_bytes"] == 14
    store.update_host_storage({"run_id": "run-one", "host_storage_bytes": 5})
    assert store.run_storage_bytes("run-one") == 8
    assert store.run_storage_bytes("other-run") == 0
    for value in (-1, True, "10"):
        with pytest.raises(ValueError):
            store.update_host_storage({"run_id": "run-one", "host_storage_bytes": value})


def test_remote_resource_settings_are_forwarded_and_reported(monkeypatch):
    from a3_research_agent.host import remote_client as launch

    monkeypatch.setenv("A3_REMOTE_MANAGED_JOBS", "0")

    monkeypatch.setenv("A3_REMOTE_HOST_JOB_CPUS", "3")
    monkeypatch.setenv("A3_REMOTE_TMP_ROOT", "/data/job-temporary")
    limits = launch.remote_limits()
    assert limits["backend"] == "process"
    assert limits["temporary_root"] == "/data/job-temporary"
    for key, value in {
        "A3_REMOTE_CONTROLLER": "/data/project/remote_jobs.py",
        "A3_REMOTE_RUN_ROOT": "/data/project/runtime",
        "A3_REMOTE_DATA_ROOT": "/data/project",
        "A3_REMOTE_PROCESS_PYTHON": "/data/project/a3/.venv/bin/python",
        "A3_REMOTE_HOST": "test-host",
    }.items():
        monkeypatch.setenv(key, value)
    calls = []

    def remote_process(arguments, **kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, '{"status":"ok"}', "")

    monkeypatch.setattr(launch.subprocess, "run", remote_process)
    assert launch.remote_call("health", {}) == {"status": "ok"}
    command = calls[0][-1]
    assert "--job-python /data/project/a3/.venv/bin/python" in command
    assert "--data-root /data/project" in command
    assert "--host-job-cpus 3" in command
    assert "--tmp-root /data/job-temporary" in command


def test_artifact_optional_smaller_byte_ceiling_rejects_before_transfer(tmp_path):
    store = job_store.JobStore(tmp_path)
    job = store.create("owner", ["true"], False, {})
    store.workspace(job).mkdir(parents=True, exist_ok=True)
    (store.workspace(job) / "frame.png").write_bytes(b"x" * 1025)
    with pytest.raises(ValueError):
        job_artifacts.read_artifact(
            store,
            {
                "experiment_id": "owner",
                "job_id": job["job_id"],
                "path": "frame.png",
                "max_bytes": 1024,
            },
        )


def test_artifact_read_refuses_directory_swap_after_path_validation(tmp_path, monkeypatch):
    store = job_store.JobStore(tmp_path / "store")
    job = store.create("owner", ["true"], False, {"frames/a.png": "public"})
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "a.png").write_text("private")
    original = job_files.safe_path

    def swap(workspace, name):
        target = original(workspace, name)
        folder = workspace / "frames"
        folder.rename(workspace / "original-frames")
        folder.symlink_to(outside, target_is_directory=True)
        return target

    patch_controller(monkeypatch, "safe_path", swap)
    with pytest.raises((OSError, ValueError)):
        job_artifacts.read_artifact(
            store, {"experiment_id": "owner", "job_id": job["job_id"], "path": "frames/a.png"}
        )


def test_controller_job_listing_is_paged_and_owner_run_scoped(tmp_path):
    store = job_store.JobStore(tmp_path)
    owned = [
        store.create("owner", ["true"], False, {}, run_id="run-one", job_id=x * 32) for x in "abc"
    ]
    store.create("outsider", ["true"], False, {}, run_id="run-one")
    store.create("owner", ["true"], False, {}, run_id="run-two")
    first = store.list_jobs({"experiment_id": "owner", "run_id": "run-one", "limit": 2})
    assert [x["job_id"] for x in first["jobs"]] == [x["job_id"] for x in owned[:2]]
    second = store.list_jobs(
        {"experiment_id": "owner", "run_id": "run-one", "after": first["next_after"]}
    )
    assert [x["job_id"] for x in second["jobs"]] == [owned[2]["job_id"]]
    assert second["next_after"] is None
    with pytest.raises(ValueError):
        store.list_jobs({"experiment_id": "owner", "limit": 201})
