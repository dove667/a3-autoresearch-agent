"""Transport services used by Host launch."""

import json
import os
import shlex
import subprocess


def remote_call(operation: str, payload: dict) -> dict:
    arguments = [
        os.environ.get("A3_REMOTE_PYTHON", "python3"),
        os.environ["A3_REMOTE_CONTROLLER"],
        "--root",
        os.environ["A3_REMOTE_RUN_ROOT"],
        "--image",
        os.environ.get("A3_REMOTE_IMAGE", ""),
    ]
    for environment, flag in (
        ("A3_REMOTE_BACKEND", "--backend"),
        ("A3_REMOTE_DATA_ROOT", "--data-root"),
        ("A3_REMOTE_PROCESS_PYTHON", "--process-python"),
        ("A3_REMOTE_A3_ROOT", "--a3-root"),
        ("A3_REMOTE_HOST_MEMORY_RESERVE_MB", "--host-memory-reserve-mb"),
        ("A3_REMOTE_HOST_JOB_MEMORY_MB", "--host-job-memory-mb"),
        ("A3_REMOTE_HOST_CPU_RESERVE", "--host-cpu-reserve"),
        ("A3_REMOTE_HOST_JOB_CPUS", "--host-job-cpus"),
        ("A3_REMOTE_DISK_RESERVE_BYTES", "--disk-reserve-bytes"),
        ("A3_REMOTE_CPUS", "--cpus"),
        ("A3_REMOTE_PIDS_LIMIT", "--pids-limit"),
        ("A3_REMOTE_TMP_ROOT", "--tmp-root"),
        ("A3_REMOTE_MAX_JOBS", "--max-jobs"),
        ("A3_REMOTE_MEMORY", "--memory"),
        ("A3_REMOTE_MAX_GPUS", "--max-gpus"),
        ("A3_REMOTE_GPU_MEMORY_MB", "--gpu-memory-mb"),
        ("A3_REMOTE_GPU_HEADROOM_MB", "--gpu-headroom-mb"),
        ("A3_REMOTE_GPU_CAPACITY_MB", "--gpu-capacity-mb"),
        ("A3_REMOTE_JOB_TIMEOUT", "--timeout"),
        ("A3_REMOTE_RUN_STORAGE_BYTES", "--run-storage-bytes"),
        ("A3_REMOTE_MODELS_ROOT", "--models-root"),
        ("A3_REMOTE_MODEL_CACHE_ROOT", "--model-cache-root"),
        ("A3_REMOTE_DATASETS_ROOT", "--datasets-root"),
    ):
        if os.environ.get(environment):
            arguments += [flag, os.environ[environment]]
    if os.environ.get("A3_REMOTE_SHARE_GPUS") == "1":
        arguments += ["--share-gpus"]
    if os.environ.get("A3_REMOTE_MANAGED_JOBS") == "1":
        arguments += ["--managed-jobs"]
    if os.environ.get("A3_REMOTE_PRESERVE_MODELS_PATH") == "1":
        arguments += ["--preserve-models-path"]
    command = shlex.join([*arguments, operation])
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            os.environ["A3_REMOTE_HOST"],
            command,
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )
    return json.loads(result.stdout)


def remote_limits() -> dict:
    native = os.environ.get("A3_REMOTE_BACKEND") == "process"
    limits = {
        "backend": os.environ.get("A3_REMOTE_BACKEND", "docker"),
        "managed_jobs": os.environ.get("A3_REMOTE_MANAGED_JOBS") == "1",
        "data_root": os.environ.get("A3_REMOTE_DATA_ROOT"),
        "process_python": os.environ.get("A3_REMOTE_PROCESS_PYTHON"),
        "host_memory_reserve_mb": int(os.environ.get("A3_REMOTE_HOST_MEMORY_RESERVE_MB", "32768")),
        "disk_reserve_bytes": int(os.environ.get("A3_REMOTE_DISK_RESERVE_BYTES", "68719476736")),
        "cpus": None if native else float(os.environ.get("A3_REMOTE_CPUS", "16")),
        "memory": None if native else os.environ.get("A3_REMOTE_MEMORY", "64g"),
        "pids_limit": None if native else int(os.environ.get("A3_REMOTE_PIDS_LIMIT", "1024")),
        "temporary_root": os.environ.get("A3_REMOTE_TMP_ROOT") or "execution-server TMPDIR",
        "max_jobs": int(os.environ.get("A3_REMOTE_MAX_JOBS", "2")),
        "timeout_seconds": int(os.environ.get("A3_REMOTE_JOB_TIMEOUT", "900")),
        "run_storage_bytes": int(os.environ.get("A3_REMOTE_RUN_STORAGE_BYTES", "0")),
        "gpu_mode": "memory_shared"
        if os.environ.get("A3_REMOTE_SHARE_GPUS") == "1"
        else "exclusive",
        "max_gpus": int(os.environ.get("A3_REMOTE_MAX_GPUS", "4")),
        "gpu_memory_mb": int(os.environ.get("A3_REMOTE_GPU_MEMORY_MB", "512")),
        "gpu_headroom_mb": int(os.environ.get("A3_REMOTE_GPU_HEADROOM_MB", "2048")),
        "gpu_capacity_mb": int(os.environ.get("A3_REMOTE_GPU_CAPACITY_MB", "0")),
        "storage_scope": "local run files plus new remote data and outputs across all identities in this run",
    }
    if limits["managed_jobs"]:
        return {
            "backend": limits["backend"],
            "managed_jobs": True,
            "timeout_seconds": limits["timeout_seconds"],
            "scheduling": "Controller-owned queue, GPU allocation and episode concurrency",
            "storage": "Use supplied data paths; deployment operator manages capacity",
        }
    return limits


def validate_remote_health(response: dict) -> None:
    if response.get("status") != "ok" or response.get("protocol_version") != 1:
        raise RuntimeError("Remote controller health or protocol check failed")


def remote_path_hints() -> dict:
    if os.environ.get("A3_REMOTE_BACKEND") == "process":
        return {
            "remote_a3": os.environ.get("A3_REMOTE_A3_ROOT"),
            "remote_workspace": "paths.workspace / A3_WORKSPACE",
            "remote_outputs": "paths.outputs / A3_OUTPUTS (persistent within a research identity)",
            "remote_models": "paths.models / A3_MODELS (when configured; treat as read-only)",
            "remote_model_cache": "paths.model_cache / A3_MODEL_CACHE (when configured)",
            "remote_datasets": "paths.datasets / A3_DATASETS (when configured; treat as read-only)",
        }
    return {
        "remote_a3": "/opt/a3",
        "remote_workspace": "/workspace",
        "remote_outputs": "/outputs (persistent within a research identity; adopted main inherits predecessor job access)",
        "remote_models": "/models (read-only, when configured)",
        "remote_model_cache": "/model-cache (read-only, when configured)",
        "remote_datasets": "/datasets (read-only, when configured)",
    }
