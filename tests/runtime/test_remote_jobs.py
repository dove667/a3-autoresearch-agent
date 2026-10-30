import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "runtime/remote/remote_jobs.py"


def load_runtime():
    assert MODULE_PATH.is_file(), "remote job controller has not been implemented"
    spec = importlib.util.spec_from_file_location("remote_jobs", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_workspace_files_reject_escape_and_symlinks(tmp_path):
    runtime = load_runtime()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    for path in ("../outside/leak", "/absolute", "linked/leak"):
        with pytest.raises(ValueError):
            runtime.write_files(workspace, {path: "blocked"})
    runtime.write_files(workspace, {"nested/input.txt": "hello"})
    assert (workspace / "nested/input.txt").read_text() == "hello"
    assert list(outside.iterdir()) == []


def test_job_ownership_is_checked_before_status_or_artifact_access(tmp_path):
    runtime = load_runtime()
    store = runtime.JobStore(tmp_path)
    job = store.create("experiment-a", ["python", "-c", "print(1)"], False, {})
    assert store.load(job["job_id"], "experiment-a")["experiment_id"] == "experiment-a"
    for owner in ("experiment-b", "", None):
        with pytest.raises(PermissionError):
            store.load(job["job_id"], owner)
    with pytest.raises(PermissionError):
        store.load("missing", "experiment-a")


def test_gpu_lease_excludes_another_controller_process(tmp_path):
    runtime = load_runtime()
    lease = runtime.GpuLease.try_acquire(tmp_path, "GPU-test")
    assert lease is not None
    code = (
        "import sys; from pathlib import Path; "
        "from remote_jobs import GpuLease; "
        'lease = GpuLease.try_acquire(Path(sys.argv[1]), "GPU-test"); '
        'print("busy" if lease is None else "acquired")'
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)],
            cwd=MODULE_PATH.parent,
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "busy"
    finally:
        lease.close()
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=MODULE_PATH.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "acquired"


def test_submit_rejects_host_options_and_malformed_commands():
    runtime = load_runtime()
    for request in (
        {"experiment_id": "a", "command": ["true"], "gpu": False, "mounts": ["/"]},
        {"experiment_id": "a", "command": "true", "gpu": False},
        {"experiment_id": "a", "command": [], "gpu": False},
        {"experiment_id": "../a", "command": ["true"], "gpu": False},
        {"experiment_id": "a", "command": ["true"], "gpu": "false"},
    ):
        with pytest.raises((ValueError, TypeError)):
            runtime.validate_submit(request)


def test_artifact_reads_owner_workspace_and_rejects_symlinks(tmp_path):
    runtime = load_runtime()
    store = runtime.JobStore(tmp_path)
    job = store.create("a", ["true"], False, {"result.json": '{"ok": true}'})
    workspace = store.workspace(job)
    outside = tmp_path / "private.txt"
    outside.write_text("secret")
    (workspace / "linked.txt").symlink_to(outside)
    result = runtime.read_artifact(
        store, {"experiment_id": "a", "job_id": job["job_id"], "path": "result.json"}
    )
    assert result["encoding"] == "base64"
    assert result["data"] == "eyJvayI6IHRydWV9"
    for path in ("../private.txt", "linked.txt", "/private.txt"):
        with pytest.raises(ValueError):
            runtime.read_artifact(
                store, {"experiment_id": "a", "job_id": job["job_id"], "path": path}
            )
    with pytest.raises(PermissionError):
        runtime.read_artifact(
            store, {"experiment_id": "b", "job_id": job["job_id"], "path": "result.json"}
        )


@pytest.mark.parametrize("operation", ["status", "cancel", "artifact"])
def test_unknown_job_cli_returns_json_error_without_revealing_ownership(tmp_path, operation):
    import json

    load_runtime()
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--root", str(tmp_path), "--image", "unused", operation],
        input=json.dumps({"experiment_id": "a", "job_id": "missing", "path": "result.json"}),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "status": "error",
        "error": "PermissionError",
        "message": "job is not accessible",
    }
