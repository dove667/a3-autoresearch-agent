"""Build fixed-permission containers and inspect their actual state."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from a3_research_agent.execution.exports.readonly import ReadonlyExports

from .store import JobStore


def docker(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=30,
    )
    if check and result.returncode:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip() or "container operation failed"
        )
    return result


def container_state(container_id: str) -> dict:
    result = docker(["inspect", "--format", "{{json .State}}", container_id], check=False)
    if result.returncode:
        if "No such object" in result.stderr or "No such container" in result.stderr:
            return {"Status": "removed", "ExitCode": None}
        raise RuntimeError("cannot inspect container state")
    return json.loads(result.stdout)


def cleanup_temporary_directory(job: dict) -> None:
    directory = job.get("temporary_directory")
    if directory:
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass


def create_container(store: JobStore, job: dict, args) -> str:
    temporary_root = getattr(args, "tmp_root", None) or os.environ.get("TMPDIR")
    temporary_root = Path(temporary_root) if temporary_root else store.root / "temporary"
    namespace = hashlib.sha256(str(store.root).encode()).hexdigest()[:16]
    temporary = temporary_root.resolve() / "a3-jobs" / namespace / job["job_id"]
    temporary.mkdir(parents=True, mode=0o700)
    (temporary / "home").mkdir(mode=0o700)
    job["temporary_directory"] = str(temporary)
    job["resource_limits"] = {
        "cpus": args.cpus,
        "memory": args.memory,
        "pids_limit": args.pids_limit,
        "swap": False,
    }
    store.save(job)
    arguments = [
        "create",
        "--name",
        f"a3-job-{job['job_id']}",
        "--label",
        f"a3.experiment={job['experiment_id']}",
        "--label",
        f"a3.job={job['job_id']}",
        "--label",
        f"a3.root={hashlib.sha256(str(store.root).encode()).hexdigest()}",
        "--read-only",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cpus",
        str(args.cpus),
        "--memory",
        args.memory,
        "--memory-swap",
        args.memory,
        "--pids-limit",
        str(args.pids_limit),
        "--shm-size",
        "256m",
        "--mount",
        f"type=bind,source={temporary},target=/job-tmp",
        "--log-driver",
        "local",
        "--log-opt",
        "max-size=1m",
        "--log-opt",
        "max-file=2",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--env",
        "USER=agent",
        "--env",
        "LOGNAME=agent",
        "--env",
        "HOME=/job-tmp/home",
        "--env",
        "TMPDIR=/job-tmp",
        "--env",
        "TMP=/job-tmp",
        "--env",
        "TEMP=/job-tmp",
        "--env",
        "XDG_CACHE_HOME=/job-tmp/cache",
        "--env",
        "MPLCONFIGDIR=/job-tmp/matplotlib",
        "--env",
        "HF_HUB_OFFLINE=1",
        "--env",
        "TRANSFORMERS_OFFLINE=1",
        "--env",
        "TORCHINDUCTOR_CACHE_DIR=/job-tmp/torchinductor",
        "--workdir",
        "/workspace",
        "--mount",
        f"type=bind,source={store.workspace(job)},target=/workspace",
    ]
    executor = Path(__file__).resolve().parents[1] / "episode_executor.py"
    arguments += [
        "--mount",
        f"type=bind,source={executor},target=/run/a3/episode_executor.py,readonly",
        "--env",
        f"A3_JOB_GPU_MEMORY_MB={job.get('gpu_memory_mb', 0)}",
        "--env",
        f"A3_JOB_GPU_UUID={job.get('gpu_uuid') or ''}",
    ]
    if job.get("run_id"):
        arguments += ["--mount", f"type=bind,source={store.outputs(job)},target=/outputs"]
    for option, target in (("models_root", "/models"), ("datasets_root", "/datasets")):
        source = getattr(args, option, None)
        if source:
            source = Path(source).resolve(strict=True)
            if not source.is_dir() or "," in str(source):
                raise ValueError("Shared asset root must be a directory without commas")
            arguments += ["--mount", f"type=bind,source={source},target={target},readonly"]
            if (
                option == "models_root"
                and getattr(args, "preserve_models_path", False)
                and str(source) != target
            ):
                arguments += ["--mount", f"type=bind,source={source},target={source},readonly"]
    cache = getattr(args, "model_cache_root", None)
    if cache:
        cache = Path(cache).resolve(strict=True)
        if not cache.is_dir() or "," in str(cache):
            raise ValueError("Model cache root must be a directory without commas")
        arguments += [
            "--mount",
            f"type=bind,source={cache},target=/model-cache,readonly",
            "--env",
            "HF_HOME=/model-cache/huggingface",
            "--env",
            "HF_HUB_CACHE=/model-cache/huggingface/hub",
            "--env",
            "TORCH_HOME=/model-cache/torch",
        ]
    exports = ReadonlyExports(store.root)
    for identifier in job.get("input_exports", []):
        source = exports.data_path(job["experiment_id"], identifier)
        arguments += ["--mount", f"type=bind,source={source},target=/inputs/{identifier},readonly"]
    if job["gpu_uuid"]:
        telemetry = store.metadata_path(job["job_id"]).parent / "episode-telemetry"
        telemetry.mkdir(mode=0o700, exist_ok=True)
        job["episode_telemetry_directory"] = str(telemetry)
        store.save(job)
        arguments += [
            "--mount",
            f"type=bind,source={telemetry},target=/run/a3/telemetry,readonly",
        ]
        # Driver libraries can be injected without their GLVND discovery descriptor.
        vendor = Path(__file__).resolve().with_name("nvidia_egl_vendor.json")
        arguments += [
            "--gpus",
            f"device={job['gpu_uuid']}",
            "--mount",
            f"type=bind,source={vendor},target=/run/a3-egl/nvidia.json,readonly",
            "--env",
            "__EGL_VENDOR_LIBRARY_FILENAMES=/run/a3-egl/nvidia.json",
        ]
    return docker([*arguments, args.image, *job["command"]]).stdout.strip()
