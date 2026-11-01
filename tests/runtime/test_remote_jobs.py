import importlib
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "runtime/remote/remote_jobs.py"


def load_runtime():
    assert MODULE_PATH.is_file(), "remote job controller has not been implemented"
    return importlib.import_module("runtime.remote.remote_jobs")


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


def test_run_storage_combines_owners_and_ignores_other_runs(tmp_path):
    runtime = load_runtime()
    store = runtime.JobStore(tmp_path)
    first = store.create("a", ["true"], False, {"data": "123"}, run_id="run-one")
    second = store.create("b", ["true"], False, {"data": "4567"}, run_id="run-one")
    store.create("a", ["true"], False, {"data": "other"}, run_id="run-two")
    assert store.run_storage_bytes("run-one") == 7
    assert store.workspace(first) != store.workspace(second)
    outside = tmp_path / "outside"
    outside.write_text("not part of quota")
    (store.workspace(first) / "link").symlink_to(outside)
    assert store.run_storage_bytes("run-one") == 7


def test_zero_timeout_means_no_wall_time_limit():
    runtime = load_runtime()
    assert runtime.job_timeout(0) is None
    assert runtime.job_timeout(12) == 12
    with pytest.raises(ValueError):
        runtime.job_timeout(-1)


def test_storage_limit_blocks_new_job_before_gpu_or_container_allocation(tmp_path, monkeypatch):
    from argparse import Namespace

    runtime = load_runtime()
    store = runtime.JobStore(tmp_path)
    store.create("old-owner", ["true"], False, {"data": "1234567"}, run_id="run-one")
    monkeypatch.setattr(runtime, "docker", lambda *a, **k: pytest.fail("Docker reached"))
    result = runtime.submit(
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


def test_storage_watcher_stops_only_its_job_and_keeps_evidence(tmp_path, monkeypatch):
    from argparse import Namespace

    runtime = load_runtime()
    store = runtime.JobStore(tmp_path)
    job = store.create("owner", ["true"], False, {"data": "12345"}, run_id="run-one")
    job.update(container_id="own-container", storage_limit_bytes=4)
    store.save(job)
    calls = []
    state = {"Status": "running", "ExitCode": None}

    def docker(arguments, **kwargs):
        calls.append(arguments)
        if arguments[0] == "stop":
            state.update(Status="exited", ExitCode=143)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(runtime, "docker", docker)
    monkeypatch.setattr(runtime, "container_state", lambda container: dict(state))
    runtime.watch(Namespace(root=tmp_path, job_id=job["job_id"], timeout=0, lock_fd=None))
    saved = store.load(job["job_id"], "owner")
    assert saved["status"] == "failed"
    assert saved["error"] == "run storage limit reached"
    assert ["stop", "--time", "10", "own-container"] in calls
    assert (store.workspace(job) / "data").read_text() == "12345"
    assert all("other-container" not in call for call in calls)


def test_shared_assets_are_read_only_and_outputs_stay_with_owner(tmp_path, monkeypatch):
    from argparse import Namespace

    runtime = load_runtime()
    store = runtime.JobStore(tmp_path / "runtime")
    job = store.create("owner", ["true"], False, {}, run_id="run-one")
    models, datasets, cache = tmp_path / "models", tmp_path / "datasets", tmp_path / "cache"
    models.mkdir()
    datasets.mkdir()
    cache.mkdir()
    calls = []
    monkeypatch.setattr(
        runtime,
        "docker",
        lambda arguments: (
            calls.append(arguments) or subprocess.CompletedProcess(arguments, 0, "container\n", "")
        ),
    )
    runtime.create_container(
        store,
        job,
        Namespace(
            cpus=2,
            memory="8g",
            pids_limit=256,
            image="test",
            models_root=models,
            datasets_root=datasets,
            model_cache_root=cache,
            preserve_models_path=True,
        ),
    )
    assert f"type=bind,source={models},target=/models,readonly" in calls[0]
    assert f"type=bind,source={datasets},target=/datasets,readonly" in calls[0]
    assert f"type=bind,source={models},target={models},readonly" in calls[0]
    assert f"type=bind,source={cache},target=/model-cache,readonly" in calls[0]
    assert "HF_HUB_CACHE=/model-cache/huggingface/hub" in calls[0]
    assert "TORCH_HOME=/model-cache/torch" in calls[0]
    assert f"type=bind,source={store.outputs(job)},target=/outputs" in calls[0]
    other = store.create("other-owner", ["true"], False, {}, run_id="run-one")
    assert store.outputs(other) != store.outputs(job)


def test_run_storage_includes_latest_host_report(tmp_path):
    runtime = load_runtime()
    store = runtime.JobStore(tmp_path)
    store.create("a", ["true"], False, {"data": "123"}, run_id="run-one")
    result = store.update_host_storage({"run_id": "run-one", "host_storage_bytes": 11})
    assert result["used_bytes"] == 14
    store.update_host_storage({"run_id": "run-one", "host_storage_bytes": 5})
    assert store.run_storage_bytes("run-one") == 8
    assert store.run_storage_bytes("other-run") == 0
    for value in (-1, True, "10"):
        with pytest.raises(ValueError):
            store.update_host_storage({"run_id": "run-one", "host_storage_bytes": value})


def test_remote_job_has_a_user_name_and_writable_training_cache(tmp_path, monkeypatch):
    from argparse import Namespace

    runtime = load_runtime()
    store = runtime.JobStore(tmp_path / "runtime")
    job = store.create("owner", ["true"], False, {}, run_id="run-one")
    calls = []
    monkeypatch.setattr(
        runtime,
        "docker",
        lambda arguments: (
            calls.append(arguments) or subprocess.CompletedProcess(arguments, 0, "container\n", "")
        ),
    )
    runtime.create_container(
        store, job, Namespace(cpus=2, memory="8g", pids_limit=256, image="test")
    )
    arguments = calls[0]
    assert "USER=agent" in arguments
    assert "LOGNAME=agent" in arguments
    assert "HOME=/tmp/agent" in arguments
    assert "HF_HUB_OFFLINE=1" in arguments
    assert "TRANSFORMERS_OFFLINE=1" in arguments
    assert arguments[arguments.index("--network") + 1] == "none"
    assert "TORCHINDUCTOR_CACHE_DIR=/outputs/.cache/torchinductor" in arguments
