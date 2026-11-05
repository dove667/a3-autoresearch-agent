import subprocess
import sys
from pathlib import Path

import pytest
from remote_test_support import patch_controller

from a3_research_agent.execution.controller import artifacts as job_artifacts
from a3_research_agent.execution.controller import docker as job_docker
from a3_research_agent.execution.controller import files as job_files
from a3_research_agent.execution.controller import lifecycle as job_lifecycle
from a3_research_agent.execution.controller import store as job_store
from a3_research_agent.execution.controller import submission as job_submission
from a3_research_agent.execution.scheduling import gpu as job_gpu

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "src/a3_research_agent/execution/controller/__main__.py"
)


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
def test_unknown_job_cli_returns_json_error_without_revealing_ownership(tmp_path, operation):
    import json

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


def test_zero_timeout_means_no_wall_time_limit():
    assert job_files.job_timeout(0) is None
    assert job_files.job_timeout(12) == 12
    with pytest.raises(ValueError):
        job_files.job_timeout(-1)


def test_storage_limit_blocks_new_job_before_gpu_or_container_allocation(tmp_path, monkeypatch):
    from argparse import Namespace

    store = job_store.JobStore(tmp_path)
    store.create("old-owner", ["true"], False, {"data": "1234567"}, run_id="run-one")
    patch_controller(monkeypatch, "docker", lambda *a, **k: pytest.fail("Docker reached"))
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


def test_storage_watcher_stops_only_its_job_and_keeps_evidence(tmp_path, monkeypatch):
    from argparse import Namespace

    store = job_store.JobStore(tmp_path)
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

    patch_controller(monkeypatch, "docker", docker)
    patch_controller(monkeypatch, "container_state", lambda container: dict(state))
    job_lifecycle.watch(Namespace(root=tmp_path, job_id=job["job_id"], timeout=0, lock_fd=None))
    saved = store.load(job["job_id"], "owner")
    assert saved["status"] == "failed"
    assert saved["error"] == "run storage limit reached"
    assert ["stop", "--time", "10", "own-container"] in calls
    assert (store.workspace(job) / "data").read_text() == "12345"
    assert all("other-container" not in call for call in calls)


def test_shared_assets_are_read_only_and_outputs_stay_with_owner(tmp_path, monkeypatch):
    from argparse import Namespace

    store = job_store.JobStore(tmp_path / "runtime")
    job = store.create("owner", ["true"], False, {}, run_id="run-one")
    models, datasets, cache = tmp_path / "models", tmp_path / "datasets", tmp_path / "cache"
    models.mkdir()
    datasets.mkdir()
    cache.mkdir()
    calls = []
    patch_controller(
        monkeypatch,
        "docker",
        lambda arguments: (
            calls.append(arguments) or subprocess.CompletedProcess(arguments, 0, "container\n", "")
        ),
    )
    job_docker.create_container(
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


def test_remote_job_has_a_user_name_and_writable_training_cache(tmp_path, monkeypatch):
    from argparse import Namespace

    store = job_store.JobStore(tmp_path / "runtime")
    job = store.create("owner", ["true"], False, {}, run_id="run-one")
    calls = []
    patch_controller(
        monkeypatch,
        "docker",
        lambda arguments: (
            calls.append(arguments) or subprocess.CompletedProcess(arguments, 0, "container\n", "")
        ),
    )
    job_docker.create_container(
        store, job, Namespace(cpus=2, memory="8g", pids_limit=256, image="test")
    )
    arguments = calls[0]
    assert "USER=agent" in arguments
    assert "LOGNAME=agent" in arguments
    assert "HOME=/job-tmp/home" in arguments
    assert "HF_HUB_OFFLINE=1" in arguments
    assert "TRANSFORMERS_OFFLINE=1" in arguments
    assert arguments[arguments.index("--network") + 1] == "none"
    assert "TORCHINDUCTOR_CACHE_DIR=/job-tmp/torchinductor" in arguments


def test_job_temporary_files_use_isolated_data_directory_and_count_toward_budget(
    tmp_path, monkeypatch
):
    from argparse import Namespace

    store = job_store.JobStore(tmp_path / "runtime")
    job = store.create("owner", ["true"], False, {}, run_id="run-one")
    temporary_root = tmp_path / "data-temporary"
    monkeypatch.setenv("TMPDIR", str(temporary_root))
    calls = []
    patch_controller(
        monkeypatch,
        "docker",
        lambda arguments: (
            calls.append(arguments) or subprocess.CompletedProcess(arguments, 0, "container\n", "")
        ),
    )
    job_docker.create_container(
        store, job, Namespace(cpus=16, memory="64g", pids_limit=1024, image="test")
    )
    arguments = calls[0]
    assert arguments[arguments.index("--cpus") + 1] == "16"
    assert arguments[arguments.index("--memory-swap") + 1] == "64g"
    assert arguments[arguments.index("--pids-limit") + 1] == "1024"
    assert "--tmpfs" not in arguments
    assert "TMPDIR=/job-tmp" in arguments
    assert "HOME=/job-tmp/home" in arguments
    assert "XDG_CACHE_HOME=/job-tmp/cache" in arguments
    directory = Path(job["temporary_directory"])
    assert directory.is_relative_to(temporary_root)
    assert f"type=bind,source={directory},target=/job-tmp" in arguments
    assert not any("target=/tmp" in x for x in arguments)
    store.save(job)
    (directory / "partial").write_bytes(b"12345")
    assert store.run_storage_bytes("run-one") == 5
    assert store.run_storage_bytes("run-other") == 0
    peer = store.create("peer", ["true"], False, {}, run_id="run-other")
    job_docker.create_container(
        store, peer, Namespace(cpus=16, memory="64g", pids_limit=1024, image="test")
    )
    peer_temporary = Path(peer["temporary_directory"])
    (peer_temporary / "keep").write_text("peer")
    job_docker.cleanup_temporary_directory(job)
    job_docker.cleanup_temporary_directory(job)
    assert (peer_temporary / "keep").read_text() == "peer"
    assert not directory.exists()
    assert store.run_storage_bytes("run-one") == 0


def test_remote_resource_settings_are_forwarded_and_reported(monkeypatch):
    from a3_research_agent.host import bootstrap as launch

    monkeypatch.setenv("A3_REMOTE_CPUS", "16")
    monkeypatch.setenv("A3_REMOTE_PIDS_LIMIT", "1024")
    monkeypatch.setenv("A3_REMOTE_MEMORY", "64g")
    monkeypatch.setenv("A3_REMOTE_TMP_ROOT", "/data/job-temporary")
    limits = launch.remote_limits()
    assert limits["cpus"] == 16
    assert limits["pids_limit"] == 1024
    assert limits["temporary_root"] == "/data/job-temporary"
    for key, value in {
        "A3_REMOTE_CONTROLLER": "/data/project/remote_jobs.py",
        "A3_REMOTE_RUN_ROOT": "/data/project/runtime",
        "A3_REMOTE_IMAGE": "test-image",
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
    assert "--cpus 16" in command
    assert "--pids-limit 1024" in command
    assert "--tmp-root /data/job-temporary" in command


def test_watcher_removes_only_exited_jobs_temporary_files(tmp_path, monkeypatch):
    from argparse import Namespace

    store = job_store.JobStore(tmp_path / "runtime")
    job = store.create("owner", ["true"], False, {}, run_id="run-one")
    temporary = tmp_path / "data-temporary"
    temporary.mkdir()
    (temporary / "scratch").write_text("disposable")
    (store.outputs(job) / "checkpoint").write_text("persistent")
    job.update(container_id="own-container", temporary_directory=str(temporary))
    store.save(job)
    patch_controller(
        monkeypatch, "docker", lambda a, **kw: subprocess.CompletedProcess(a, 0, "", "")
    )
    patch_controller(monkeypatch, "container_state", lambda c: {"Status": "exited", "ExitCode": 0})
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "0", "")
    )
    job_lifecycle.watch(Namespace(root=store.root, job_id=job["job_id"], timeout=0, lock_fd=None))
    assert not temporary.exists()
    assert (store.outputs(job) / "checkpoint").read_text() == "persistent"
    assert store.load(job["job_id"], "owner")["status"] == "completed"


@pytest.mark.parametrize("gpu", [False, True])
def test_gpu_jobs_select_readonly_nvidia_egl_vendor(tmp_path, monkeypatch, gpu):
    import json
    from argparse import Namespace

    store = job_store.JobStore(tmp_path / "runtime")
    job = store.create("owner", ["true"], gpu, {})
    job["gpu_uuid"] = "GPU-selected" if gpu else None
    calls = []
    patch_controller(
        monkeypatch,
        "docker",
        lambda arguments: (
            calls.append(arguments) or subprocess.CompletedProcess(arguments, 0, "container\n", "")
        ),
    )
    job_docker.create_container(
        store, job, Namespace(cpus=16, memory="64g", pids_limit=1024, image="test")
    )
    arguments = calls[0]
    vendor = (
        Path(__file__).resolve().parents[2]
        / "src/a3_research_agent/execution/controller/nvidia_egl_vendor.json"
    )
    mount = f"type=bind,source={vendor},target=/run/a3-egl/nvidia.json,readonly"
    setting = "__EGL_VENDOR_LIBRARY_FILENAMES=/run/a3-egl/nvidia.json"
    assert (mount in arguments) is gpu
    assert (setting in arguments) is gpu
    if gpu:
        assert json.loads(vendor.read_text()) == {
            "file_format_version": "1.0.0",
            "ICD": {"library_path": "libEGL_nvidia.so.0"},
        }
        assert arguments[arguments.index("--gpus") + 1] == "device=GPU-selected"
    assert arguments[arguments.index("--network") + 1] == "none"
    assert "ALL" in arguments and "no-new-privileges" in arguments


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
