"""Trusted Linux process jobs with data-disk paths and durable lifecycle metadata.

This backend deliberately uses the execution account's filesystem/network authority.
It is not a security sandbox. Agent credentials remain on the local Host.
"""

import base64
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .files import ACTIVE_STATES, atomic_json, public_job_status


def different_device_from_system(path: Path) -> bool:
    return path.stat().st_dev != Path("/").stat().st_dev


def validate_data_paths(data_root: Path, paths: list[Path]) -> None:
    root = data_root.resolve(strict=True)
    if not different_device_from_system(root):
        raise ValueError("Process jobs require a separate data disk")
    for path in paths:
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("Managed job paths must be below the data root")
        ancestor = resolved
        while not ancestor.exists():
            ancestor = ancestor.parent
        if ancestor.stat().st_dev != root.stat().st_dev:
            raise ValueError("Managed job paths must stay on the configured data disk")


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def read_process(pid: int) -> dict | None:
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text()
        fields = text[text.rfind(")") + 2 :].split()
        return {
            "pid": int(pid),
            "state": fields[0],
            "pgrp": int(fields[2]),
            "start_ticks": int(fields[19]),
            "rss_pages": int(fields[21]),
        }
    except (FileNotFoundError, ProcessLookupError):
        return None


def is_running(job: dict) -> bool:
    identity = job.get("process")
    if not identity or identity["boot_id"] != boot_id():
        return False
    current = read_process(identity["pid"])
    return bool(
        current
        and current["start_ticks"] == identity["start_ticks"]
        and current["state"] not in {"Z", "X"}
    )


def process_ids(job: dict) -> list[int]:
    """Find the root group and detached episode descendants via their inherited job marker."""
    identity = job.get("process")
    if not identity or identity["boot_id"] != boot_id():
        return []
    root = read_process(identity["pid"])
    if root and root["start_ticks"] != identity["start_ticks"]:
        return []
    marker = f"A3_JOB_ID={job['job_id']}".encode()
    result = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            record = read_process(int(path.name))
            if (
                not record
                or record["state"] in {"Z", "X"}
                or record["start_ticks"] < identity["start_ticks"]
            ):
                continue
            if marker in (path / "environ").read_bytes().split(b"\0"):
                result.append(record["pid"])
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return result


def signal_job(job: dict, sig: int) -> None:
    # Check each PID again immediately before signaling; never kill an unverified group.
    for pid in process_ids(job):
        try:
            descriptor = os.pidfd_open(pid)
            try:
                marker = f"A3_JOB_ID={job['job_id']}".encode()
                if marker in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
                    signal.pidfd_send_signal(descriptor, sig)
            finally:
                os.close(descriptor)
        except (FileNotFoundError, ProcessLookupError):
            pass


def stop(job: dict, grace: float = 5) -> None:
    signal_job(job, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while process_ids(job) and time.monotonic() < deadline:
        time.sleep(0.1)
    signal_job(job, signal.SIGKILL)
    deadline = time.monotonic() + 3
    while process_ids(job) and time.monotonic() < deadline:
        time.sleep(0.05)
    if process_ids(job):
        raise RuntimeError("Owned job processes did not exit")


def prepare(store, job: dict, args) -> None:
    job.setdefault(
        "controller_options",
        {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
    )
    python = Path(args.process_python).resolve(strict=True)
    temporary = Path(args.tmp_root or store.root / "temporary") / job["job_id"]
    paths = {
        "workspace": str(store.workspace(job)),
        "outputs": str(store.outputs(job)),
        "temporary": str(temporary),
        "shared_cache": str(store.root / "shared-cache" / job["run_id"]),
        "python": str(Path(args.process_python).absolute()),
        "episode_executor": str(Path(__file__).resolve().parents[1] / "episode_executor.py"),
        "telemetry": str(
            store.metadata_path(job["job_id"]).parent / "episode-telemetry/resources.json"
        ),
    }
    validate_data_paths(Path(args.data_root), [store.root, temporary, python])
    for option, name in (
        ("models_root", "models"),
        ("datasets_root", "datasets"),
        ("model_cache_root", "model_cache"),
    ):
        if getattr(args, option, None):
            paths[name] = str(Path(getattr(args, option)).resolve(strict=True))
    if getattr(args, "a3_root", None):
        paths["a3"] = str(Path(args.a3_root).resolve(strict=True))
    from a3_research_agent.execution.exports.readonly import ReadonlyExports

    exports = ReadonlyExports(store.root)
    inputs = {
        identifier: str(exports.data_path(job["experiment_id"], identifier))
        for identifier in job.get("input_exports", [])
    }
    validate_data_paths(
        Path(args.data_root),
        [Path(p) for k, p in paths.items() if k != "python"] + [Path(p) for p in inputs.values()],
    )
    temporary.mkdir(parents=True, exist_ok=True, mode=0o700)
    Path(paths["shared_cache"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    job.update(
        backend="process",
        paths=paths,
        input_paths=inputs,
        temporary_directory=str(temporary),
        host_memory_mb=args.host_job_memory_mb,
        host_cpus=args.host_job_cpus,
        resource_limits={"cpus": None, "memory": None, "admission": "whole_host"},
    )
    if job.get("gpu_uuid"):
        telemetry = Path(paths["telemetry"]).parent
        telemetry.mkdir(parents=True, exist_ok=True)
        job["episode_telemetry_directory"] = str(telemetry)
    atomic_json(
        store.workspace(job) / "a3-job-context.json",
        {"version": "0.1.0", "backend": "process", "paths": paths, "input_exports": inputs},
    )
    store.save(job)


def environment(job: dict) -> dict:
    paths = job["paths"]
    temporary = Path(paths["temporary"])
    env = {
        "PATH": str(Path(paths["python"]).parent)
        + ":/usr/local/cuda/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "A3_JOB_ID": job["job_id"],
        "A3_JOB_RESOURCE_BACKEND": "process",
        "A3_JOB_MANAGED": "1" if job.get("managed") else "0",
        "A3_JOB_GPU_MEMORY_MB": str(job.get("gpu_memory_mb", 0)),
        "A3_JOB_GPU_UUID": job.get("gpu_uuid") or "",
        "CUDA_VISIBLE_DEVICES": job.get("gpu_uuid") or "",
        "A3_INPUT_EXPORTS_JSON": json.dumps(job["input_paths"]),
        "A3_JOB_TELEMETRY": paths["telemetry"],
        "MUJOCO_GL": "egl",
        "PYOPENGL_PLATFORM": "egl",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "A3_HOST_MEMORY_RESERVE_MB": str(
            job["controller_options"].get("host_memory_reserve_mb", 32768)
        ),
    }
    for name, suffix in {
        "HOME": "home",
        "TMPDIR": "tmp",
        "TMP": "tmp",
        "TEMP": "tmp",
        "XDG_CACHE_HOME": "cache",
        "XDG_CONFIG_HOME": "config",
        "XDG_DATA_HOME": "data",
        "HF_HOME": "huggingface",
        "HF_HUB_CACHE": "huggingface/hub",
        "TORCH_HOME": "torch",
        "TORCHINDUCTOR_CACHE_DIR": "torchinductor",
        "TRITON_CACHE_DIR": "triton",
        "CUDA_CACHE_PATH": "cuda",
        "NUMBA_CACHE_DIR": "numba",
        "MPLCONFIGDIR": "matplotlib",
        "PIP_CACHE_DIR": "pip",
        "UV_CACHE_DIR": "uv",
    }.items():
        target = temporary / suffix
        target.mkdir(parents=True, exist_ok=True)
        env[name] = str(target)
    for name, path in paths.items():
        env["A3_" + name.upper()] = path
    return env


def command(job: dict) -> list[str]:
    result = list(job["command"])
    if result[0] in {"python", "python3"}:
        result[0] = job["paths"]["python"]
    elif result[0].startswith("/opt/a3/"):
        raise ValueError("Process jobs use A3_PYTHON or python, not container interpreter paths")
    return result


def logs(store, job: dict) -> str:
    result = []
    for name in ("stdout.log", "stderr.log"):
        path = store.metadata_path(job["job_id"]).parent / name
        if path.exists():
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 65536))
                result.append(stream.read().decode(errors="replace"))
    return "".join(result)[-65536:]


def archive(store, job: dict) -> None:
    from .lifecycle import durable_json

    directory = store.metadata_path(job["job_id"]).parent
    contents = {}
    for name in ("stdout", "stderr"):
        path = directory / f"{name}.log"
        with path.open("rb") if path.exists() else open(os.devnull, "rb") as stream:
            stream.seek(max(0, path.stat().st_size - 4 * 1024 * 1024) if path.exists() else 0)
            contents[name + "_base64"] = base64.b64encode(stream.read()).decode()
    durable_json(
        directory / "terminal-archive.json",
        {
            "backend": "process",
            "managed": job.get("managed", False),
            "archived_at": time.time(),
            "exit_code": job.get("exit_code"),
            **contents,
        },
    )
    atomic_json(
        directory / "cleanup.json",
        {
            "status": "removed",
            "backend": "process",
            "meaning": "owned processes exited; files retained",
        },
    )


def watch_attempt(args) -> None:
    from a3_research_agent.execution.scheduling import host_resources

    from .episode_telemetry import EpisodeTelemetry
    from .lifecycle import lifecycle_lock
    from .store import JobStore

    store = JobStore(args.root)
    job = json.loads(store.metadata_path(args.job_id).read_text())
    directory = store.metadata_path(job["job_id"]).parent
    process, telemetry = None, None
    try:
        with lifecycle_lock(store, job):
            job = store.load(job["job_id"], job["experiment_id"])
            watcher = read_process(os.getpid())
            job["watcher"] = {
                "pid": os.getpid(),
                "start_ticks": watcher["start_ticks"],
                "boot_id": boot_id(),
            }
            store.save(job)
            if (directory / "cancel_requested").exists():
                return
            env = environment(job)
            with (
                (directory / "stdout.log").open("wb") as stdout,
                (directory / "stderr.log").open("wb") as stderr,
            ):
                process = subprocess.Popen(
                    command(job),
                    cwd=job["paths"]["workspace"],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
            identity = read_process(process.pid)
            if identity is None:
                raise RuntimeError("Could not record process identity")
            job.update(
                status="running",
                started_at=time.time(),
                process={
                    "pid": process.pid,
                    "start_ticks": identity["start_ticks"],
                    "boot_id": boot_id(),
                },
            )
            store.save(job)
        if job.get("gpu_uuid"):
            telemetry = EpisodeTelemetry(job)
            telemetry.start()
        started, last_storage = time.monotonic(), 0
        while process.poll() is None:
            if (directory / "cancel_requested").exists():
                stop(job)
                break
            if args.timeout and time.monotonic() - started >= args.timeout:
                job["error"] = "job timed out"
                stop(job)
                break
            if time.monotonic() - last_storage >= 5:
                options = job["controller_options"]
                disk_free = host_resources.sample(Path(options["data_root"]))["disk_free_bytes"]
                limit = job.get("storage_limit_bytes", 0)
                if disk_free < options["disk_reserve_bytes"] or (
                    limit and store.run_storage_bytes(job["run_id"]) >= limit
                ):
                    job["error"] = "data disk headroom or run storage limit reached"
                    stop(job)
                    break
                last_storage = time.monotonic()
            time.sleep(0.2)
        job["exit_code"] = process.wait(timeout=5)
        remaining = process_ids(job)
        if remaining:
            stop(job)
            job["error"] = "job leader exited with background descendants; cleaned"
        job["status"] = "completed" if job["exit_code"] == 0 and not job.get("error") else "failed"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if process and process.poll() is None:
            if job.get("process"):
                stop(job)
            else:
                process.kill()
            process.wait(timeout=5)
        job.update(status="failed", error=str(error))
    finally:
        if telemetry:
            telemetry.stop()
        with lifecycle_lock(store, job):
            if (directory / "cancel_requested").exists():
                job["status"] = "cancelled"
            job.update(finished_at=time.time(), watcher_ended=True)
            store.save(job)
            archive(store, job)
        if args.lock_fd is not None:
            (store.root / "locks" / f"{job['gpu_uuid']}.json").unlink(missing_ok=True)
            os.close(args.lock_fd)


def status(store, request: dict) -> dict:
    from .lifecycle import lifecycle_lock

    job = store.load(request.get("job_id"), request.get("experiment_id"))
    with lifecycle_lock(store, job):
        job = store.load(job["job_id"], job["experiment_id"])
        if job["status"] == "running" and not is_running(job):
            # The watcher records the exit code. Never infer successful completion from a missing PID.
            watcher = job.get("watcher")
            if not watcher or not is_running({"process": watcher}):
                stop(job)
                job.update(
                    status="failed",
                    error="watcher disappeared; exit status unknown",
                    finished_at=time.time(),
                )
                store.save(job)
                archive(store, job)
        return {
            "job_id": job["job_id"],
            "backend": "process",
            "managed": job.get("managed", False),
            "status": public_job_status(job),
            "gpu_uuid": job.get("gpu_uuid"),
            "gpu_shared": job.get("gpu_shared", False),
            "gpu_memory_mb": job.get("gpu_memory_mb", 0),
            "exit_code": job.get("exit_code"),
            "paths": job.get("paths"),
            "input_paths": job.get("input_paths"),
            "output": logs(store, job),
            "error": job.get("error"),
            "retry_count": job.get("retry_count", 0),
            "retry": job.get("retry"),
            "attempts": job.get("attempts", []),
        }


def cancel(store, request: dict) -> dict:
    from .lifecycle import lifecycle_lock

    job = store.load(request.get("job_id"), request.get("experiment_id"))
    with lifecycle_lock(store, job):
        job = store.load(job["job_id"], job["experiment_id"])
        if job["status"] in ACTIVE_STATES | {"queued"} or job.get("retry_pending"):
            (store.metadata_path(job["job_id"]).parent / "cancel_requested").touch()
            stop(job)
            job.update(status="cancelled", finished_at=time.time())
            store.save(job)
    return status(store, request)
