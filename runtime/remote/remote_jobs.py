"""Submit isolated container jobs using a host-controlled runtime directory."""

import argparse
import base64
import csv
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath

if __package__:
    from .readonly_exports import ReadonlyExports
else:
    from readonly_exports import ReadonlyExports

ACTIVE_STATES = {"created", "running", "restarting", "paused"}
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_INPUT_BYTES = 20 * 1024 * 1024


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def safe_path(workspace: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError("file path must be a nonempty relative path")
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or relative == PurePosixPath("."):
        raise ValueError("file path escapes workspace")
    target = workspace
    for part in relative.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError("symlink paths are not allowed")
    if not target.resolve().is_relative_to(workspace.resolve()):
        raise ValueError("file path escapes workspace")
    return target


def write_files(workspace: Path, files: dict) -> None:
    if not isinstance(files, dict) or len(files) > 128:
        raise ValueError("files must contain at most 128 UTF-8 files")
    targets = []
    total = 0
    for name, content in files.items():
        if not isinstance(content, str):
            raise TypeError("file contents must be UTF-8 strings")
        total += len(content.encode("utf-8"))
        targets.append((safe_path(workspace, name), content))
    if total > MAX_INPUT_BYTES:
        raise ValueError("input files exceed size limit")
    for target, content in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def validate_submit(request: dict) -> None:
    if set(request) - {
        "experiment_id",
        "run_id",
        "command",
        "gpu",
        "gpu_memory_mb",
        "files",
        "input_exports",
    }:
        raise ValueError("unknown submit fields")
    owner = request.get("experiment_id")
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", owner):
        raise ValueError("invalid experiment_id")
    run_id = request.get("run_id", owner)
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise ValueError("invalid run_id")
    command = request.get("command")
    if not isinstance(command, list) or not command or len(command) > 256:
        raise ValueError("command must be a nonempty argument list")
    if any(not isinstance(value, str) or "\x00" in value for value in command) or not command[0]:
        raise ValueError("command contains invalid arguments")
    if not isinstance(request.get("gpu"), bool):
        raise TypeError("gpu must be a boolean")
    memory = request.get("gpu_memory_mb")
    if memory is not None and (type(memory) is not int or memory < 1):
        raise ValueError("gpu_memory_mb must be a positive integer")
    if not isinstance(request.get("files", {}), dict):
        raise TypeError("files must be an object")
    inputs = request.get("input_exports", [])
    if (
        not isinstance(inputs, list)
        or len(inputs) > 64
        or any(not isinstance(x, str) for x in inputs)
        or len(set(inputs)) != len(inputs)
    ):
        raise ValueError("input_exports must contain at most 64 export IDs")


class JobStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "jobs").mkdir(exist_ok=True)
        (self.root / "locks").mkdir(exist_ok=True)

    def metadata_path(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise PermissionError("job is not accessible")
        return self.root / "jobs" / job_id / "job.json"

    def load(self, job_id: str, experiment_id: str) -> dict:
        try:
            job = json.loads(self.metadata_path(job_id).read_text())
        except FileNotFoundError:
            raise PermissionError("job is not accessible") from None
        if not experiment_id or job["experiment_id"] != experiment_id:
            raise PermissionError("job is not accessible")
        return job

    def save(self, job: dict) -> None:
        atomic_json(self.metadata_path(job["job_id"]), job)

    def workspace(self, job: dict) -> Path:
        if job.get("run_id"):
            return self.run_directory(job["run_id"]) / job["experiment_id"] / "jobs" / job["job_id"]
        return self.root / "experiments" / job["experiment_id"] / job["job_id"]

    def run_directory(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
            raise ValueError("invalid run_id")
        return self.root / "runs" / run_id

    def outputs(self, job: dict) -> Path:
        return self.run_directory(job["run_id"]) / job["experiment_id"] / "outputs"

    def run_storage_bytes(self, run_id: str) -> int:
        report = self.root / "run-budgets" / f"{run_id}.json"
        total = (
            json.loads(report.read_text()).get("host_storage_bytes", 0) if report.exists() else 0
        )
        roots = [self.run_directory(run_id)]
        for metadata in (self.root / "jobs").glob("*/job.json"):
            job = json.loads(metadata.read_text())
            if job.get("run_id") == run_id and job.get("temporary_directory"):
                roots.append(Path(job["temporary_directory"]))
        for root in roots:
            total += directory_storage_bytes(root)
        return total

    def update_host_storage(self, request: dict) -> dict:
        run_id = request.get("run_id")
        self.run_directory(run_id)
        used = request.get("host_storage_bytes")
        if type(used) is not int or used < 0:
            raise ValueError("host_storage_bytes must be a nonnegative integer")
        directory = self.root / "run-budgets"
        directory.mkdir(exist_ok=True)
        atomic_json(
            directory / f"{run_id}.json",
            {
                "host_storage_bytes": used,
                "reported_at": time.time(),
            },
        )
        return {"run_id": run_id, "used_bytes": self.run_storage_bytes(run_id)}

    def create(
        self,
        experiment_id: str,
        command: list[str],
        gpu: bool,
        files: dict,
        *,
        run_id: str | None = None,
    ) -> dict:
        job = {
            "job_id": uuid.uuid4().hex,
            "experiment_id": experiment_id,
            "command": command,
            "gpu": gpu,
            "gpu_uuid": None,
            "status": "created",
            "created_at": time.time(),
            "container_id": None,
            **({"run_id": run_id} if run_id else {}),
        }
        directory = self.metadata_path(job["job_id"]).parent
        directory.mkdir(mode=0o700)
        self.workspace(job).mkdir(parents=True, mode=0o700)
        if run_id:
            self.outputs(job).mkdir(parents=True, exist_ok=True, mode=0o700)
        write_files(self.workspace(job), files)
        self.save(job)
        return job


def job_timeout(seconds: int) -> int | None:
    if seconds < 0:
        raise ValueError("timeout must be nonnegative; zero means unlimited")
    return seconds or None


class GpuLease:
    def __init__(self, path: Path, descriptor: int):
        self.path = path
        self.descriptor = descriptor

    @classmethod
    def try_acquire(cls, directory: Path, gpu_uuid: str):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{gpu_uuid}.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return cls(path, descriptor)

    def close(self):
        os.close(self.descriptor)


def docker(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, check=False, timeout=30
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


def read_artifact(store: JobStore, request: dict) -> dict:
    job = store.load(request.get("job_id"), request.get("experiment_id"))
    target = safe_path(store.workspace(job), request.get("path"))
    if not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("artifact must be a regular file of at most 32 MiB")
    data = target.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("artifact exceeds size limit")
    return {"path": request["path"], "encoding": "base64", "data": base64.b64encode(data).decode()}


def acquire_gpu(store: JobStore, allowed: set[str] | None):
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,uuid,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    for index, gpu_uuid, memory, utilization in csv.reader(
        result.stdout.splitlines(), skipinitialspace=True
    ):
        if allowed is not None and index not in allowed and gpu_uuid not in allowed:
            continue
        if float(memory) > 128 or float(utilization) > 5:
            continue
        lease = GpuLease.try_acquire(store.root / "locks", gpu_uuid)
        if lease is None:
            continue
        record_path = lease.path.with_suffix(".json")
        if record_path.exists():
            previous = json.loads(record_path.read_text())
            if container_state(previous["container_id"])["Status"] in ACTIVE_STATES:
                lease.close()
                continue
        return gpu_uuid, lease
    return None, None


def gpu_devices() -> list[dict]:
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,uuid,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return [
        {
            "index": index,
            "uuid": gpu,
            "total_mb": float(total),
            "used_mb": float(used),
            "utilization": float(utilization),
        }
        for index, gpu, total, used, utilization in csv.reader(
            result.stdout.splitlines(), skipinitialspace=True
        )
    ]


def gpu_process_memory() -> dict[tuple[str, int], float]:
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return {
        (gpu, int(pid)): float(memory)
        for gpu, pid, memory in csv.reader(result.stdout.splitlines(), skipinitialspace=True)
    }


def container_gpu_memory(job: dict, processes: dict) -> float:
    result = docker(["top", job["container_id"], "-eo", "pid"], check=False)
    if result.returncode:
        if container_state(job["container_id"])["Status"] != "running":
            return 0
        raise RuntimeError("Cannot inspect a running job's GPU processes")
    pids = [int(line.strip()) for line in result.stdout.splitlines()[1:] if line.strip()]
    return sum(processes.get((job["gpu_uuid"], pid), 0) for pid in pids)


def select_shared_gpu(
    store: JobStore, args, jobs: list[dict], memory_mb: int, *, diagnostics: dict | None = None
) -> str | None:
    """Choose a device under the scheduler lock and explain every admission decision."""
    active_devices = {job["gpu_uuid"] for job in jobs if job.get("gpu_uuid")}
    report = diagnostics if diagnostics is not None else {}
    report.update(
        requested_memory_mb=memory_mb,
        active_gpu_count=len(active_devices),
        max_gpus=args.max_gpus,
        selected_gpu_uuid=None,
        devices=[],
    )
    processes = gpu_process_memory()
    allowed = set(args.gpu_ids.split(",")) if args.gpu_ids else None
    candidates = []
    for device in gpu_devices():
        gpu = device["uuid"]
        decision = {**device, "reason": "eligible"}
        report["devices"].append(decision)
        if allowed is not None and gpu not in allowed and device["index"] not in allowed:
            decision["reason"] = "device_not_allowed"
            continue
        occupants = [job for job in jobs if job.get("gpu_uuid") == gpu]
        if any(not job.get("gpu_shared") for job in occupants):
            decision["reason"] = "exclusive_project_job"
            continue
        if gpu not in active_devices and len(active_devices) >= args.max_gpus:
            decision["reason"] = "physical_device_limit"
            continue
        probe = GpuLease.try_acquire(store.root / "locks", gpu)
        if probe is None:
            decision["reason"] = "exclusive_lease_locked"
            continue
        probe.close()
        reserved = sum(job["gpu_memory_mb"] for job in occupants)
        pending = sum(
            max(0, job["gpu_memory_mb"] - container_gpu_memory(job, processes)) for job in occupants
        )
        physical_capacity = device["total_mb"] - args.gpu_headroom_mb
        capacity = physical_capacity
        configured = getattr(args, "gpu_capacity_mb", 0)
        if configured:
            capacity = min(capacity, configured)
        available = max(
            0, min(capacity - reserved, physical_capacity - device["used_mb"] - pending)
        )
        decision.update(
            capacity_mb=capacity,
            physical_capacity_mb=physical_capacity,
            reserved_mb=reserved,
            pending_mb=pending,
            available_mb=available,
            headroom_mb=args.gpu_headroom_mb,
        )
        if available >= memory_mb:
            candidates.append((available, gpu))
        else:
            decision["reason"] = "insufficient_memory"
    selected = max(candidates)[1] if candidates else None
    report["selected_gpu_uuid"] = selected
    for decision in report["devices"]:
        if decision["uuid"] == selected:
            decision["reason"] = "selected"
    return selected


def record_scheduler_event(store: JobStore, request: dict, result: dict) -> None:
    """Save allocation evidence without commands, input files or credentials."""
    event = {
        "time": time.time(),
        "experiment_id": request["experiment_id"],
        "run_id": request.get("run_id"),
        "result": result,
    }
    with (store.root / "scheduler-events.jsonl").open("a") as stream:
        stream.write(json.dumps(event) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def directory_storage_bytes(root: Path) -> int:
    total = 0
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = [name for name in names if not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            try:
                if not path.is_symlink() and path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass  # A running job may replace or remove a file during the scan.
    return total


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
        arguments += ["--gpus", f"device={job['gpu_uuid']}"]
    return docker([*arguments, args.image, *job["command"]]).stdout.strip()


def active_jobs(store: JobStore, *, input_export: str | None = None) -> list[dict]:
    jobs = []
    for path in (store.root / "jobs").glob("*/job.json"):
        job = json.loads(path.read_text())
        if input_export is not None and input_export not in job.get("input_exports", []):
            continue
        if job["container_id"] and container_state(job["container_id"])["Status"] in ACTIVE_STATES:
            jobs.append(job)
    return jobs


def count_active_jobs(store: JobStore) -> int:
    return len(active_jobs(store))


def submit(store: JobStore, request: dict, args) -> dict:
    validate_submit(request)
    exports = ReadonlyExports(store.root)
    for identifier in request.get("input_exports", []):
        exports.data_path(request["experiment_id"], identifier)
    lease = None
    container_id = None
    with (store.root / "locks" / "scheduler.lock").open("a") as scheduler:
        fcntl.flock(scheduler, fcntl.LOCK_EX)
        limit = getattr(args, "run_storage_bytes", 0)
        run_id = request.get("run_id")
        if limit and not run_id:
            raise ValueError("A storage-limited deployment requires a Host-provided run_id")
        if limit:
            used = store.run_storage_bytes(run_id)
            contents = request.get("files", {}).values()
            if any(not isinstance(text, str) for text in contents):
                raise TypeError("file contents must be UTF-8 strings")
            uploaded = sum(len(text.encode()) for text in contents)
            if used + uploaded >= limit:
                return {
                    "job_id": None,
                    "status": "storage_limit",
                    "used_bytes": used,
                    "limit_bytes": limit,
                }
        if args.max_jobs and count_active_jobs(store) >= args.max_jobs:
            result = {
                "job_id": None,
                "status": "resource_busy",
                "reason": "job_count_limit",
                "max_jobs": args.max_jobs,
                "retry_after_seconds": 2,
            }
            record_scheduler_event(store, request, result)
            return result
        gpu_uuid = None
        allocation = {}
        shared = bool(getattr(args, "share_gpus", False))
        memory_mb = request.get("gpu_memory_mb") or getattr(args, "gpu_memory_mb", 512)
        if request["gpu"]:
            if shared:
                capacity = getattr(args, "gpu_capacity_mb", 0)
                if capacity and memory_mb > capacity:
                    result = {
                        "job_id": None,
                        "status": "resource_unavailable",
                        "reason": "requested GPU memory exceeds the per-device capacity",
                        "gpu_memory_mb": memory_mb,
                        "gpu_capacity_mb": capacity,
                    }
                    record_scheduler_event(store, request, result)
                    return result
                gpu_uuid = select_shared_gpu(
                    store, args, active_jobs(store), memory_mb, diagnostics=allocation
                )
            else:
                allowed = set(args.gpu_ids.split(",")) if args.gpu_ids else None
                gpu_uuid, lease = acquire_gpu(store, allowed)
            if gpu_uuid is None:
                result = {
                    "job_id": None,
                    "status": "resource_busy",
                    "reason": "no_eligible_gpu",
                    "retry_after_seconds": 2,
                    **({"gpu_allocation": allocation} if shared else {}),
                }
                record_scheduler_event(store, request, result)
                return result
        try:
            if request["gpu"]:
                record_scheduler_event(
                    store,
                    request,
                    {
                        "status": "gpu_selected",
                        "gpu_uuid": gpu_uuid,
                        **({"gpu_allocation": allocation} if allocation else {}),
                    },
                )
            job = store.create(
                request["experiment_id"],
                request["command"],
                request["gpu"],
                request.get("files", {}),
                run_id=run_id,
            )
            job["storage_limit_bytes"] = limit
            job["input_exports"] = request.get("input_exports", [])
            job["gpu_uuid"] = gpu_uuid
            job["gpu_shared"] = shared and request["gpu"]
            job["gpu_memory_mb"] = memory_mb if request["gpu"] else 0
            if allocation:
                job["gpu_allocation"] = allocation
            container_id = create_container(store, job, args)
            job["container_id"] = container_id
            store.save(job)
            if lease:
                atomic_json(
                    lease.path.with_suffix(".json"),
                    {
                        "job_id": job["job_id"],
                        "container_id": container_id,
                    },
                )
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--watch",
                "--root",
                str(store.root),
                "--job-id",
                job["job_id"],
                "--timeout",
                str(args.timeout),
            ]
            if lease:
                command += ["--lock-fd", str(lease.descriptor)]
            with (store.metadata_path(job["job_id"]).parent / "watcher.log").open("ab") as log:
                subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    pass_fds=(lease.descriptor,) if lease else (),
                )
            result = {
                "job_id": job["job_id"],
                "status": "created",
                "gpu_uuid": gpu_uuid,
                **({"gpu_allocation": allocation} if allocation else {}),
            }
            return result
        except Exception:
            if container_id:
                docker(["rm", "-f", container_id], check=False)
            stopped = (
                not container_id or container_state(container_id)["Status"] not in ACTIVE_STATES
            )
            if stopped and "job" in locals() and job.get("temporary_directory"):
                cleanup_temporary_directory(job)
            raise
        finally:
            if lease:
                lease.close()


def watch(args) -> None:
    store = JobStore(args.root)
    job = json.loads(store.metadata_path(args.job_id).read_text())
    cancel_path = store.metadata_path(args.job_id).parent / "cancel_requested"
    try:
        docker(["start", job["container_id"]])
        job["status"] = "running"
        store.save(job)
        timeout = job_timeout(args.timeout)
        if job.get("storage_limit_bytes"):
            started = time.monotonic()
            while True:
                state = container_state(job["container_id"])
                used = store.run_storage_bytes(job["run_id"])
                if used >= job["storage_limit_bytes"]:
                    if state["Status"] in ACTIVE_STATES:
                        docker(["stop", "--time", "10", job["container_id"]], check=False)
                    job.update(error="run storage limit reached", used_bytes=used)
                    job["exit_code"] = container_state(job["container_id"]).get("ExitCode")
                    break
                if state["Status"] not in ACTIVE_STATES:
                    job["exit_code"] = state.get("ExitCode")
                    break
                if timeout is not None and time.monotonic() - started >= timeout:
                    raise subprocess.TimeoutExpired("container job", timeout)
                time.sleep(1)
        else:
            result = subprocess.run(
                ["docker", "wait", job["container_id"]],
                capture_output=True,
                text=True,
                check=True,
                timeout=timeout,
            )
            job["exit_code"] = int(result.stdout.strip())
        job["status"] = "completed" if job["exit_code"] == 0 and not job.get("error") else "failed"
    except subprocess.TimeoutExpired:
        docker(["kill", job["container_id"]], check=False)
        job.update(status="failed", error="job timed out")
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        job.update(status="failed", error=str(error))
    finally:
        if cancel_path.exists():
            job["status"] = "cancelled"
        job["finished_at"] = time.time()
        store.save(job)
        if container_state(job["container_id"])["Status"] in ACTIVE_STATES:
            docker(["rm", "-f", job["container_id"]], check=False)
        if container_state(job["container_id"])["Status"] not in ACTIVE_STATES:
            cleanup_temporary_directory(job)
        if args.lock_fd is not None:
            lease_path = store.root / "locks" / f"{job['gpu_uuid']}.json"
            if container_state(job["container_id"])["Status"] not in ACTIVE_STATES:
                lease_path.unlink(missing_ok=True)
            os.close(args.lock_fd)


def status(store: JobStore, request: dict) -> dict:
    job = store.load(request.get("job_id"), request.get("experiment_id"))
    state = container_state(job["container_id"])
    current = state["Status"]
    if current not in ACTIVE_STATES:
        current = job["status"]
        if current in ACTIVE_STATES:
            current = "completed" if state.get("ExitCode") == 0 else "failed"
    output = docker(["logs", "--tail", "200", job["container_id"]], check=False)
    return {
        "job_id": job["job_id"],
        "status": current,
        "gpu_uuid": job["gpu_uuid"],
        "gpu_shared": job.get("gpu_shared", False),
        "gpu_memory_mb": job.get("gpu_memory_mb"),
        "exit_code": state.get("ExitCode"),
        "output": (output.stdout + output.stderr)[-65536:],
        **({"error": job["error"]} if "error" in job else {}),
    }


def cancel(store: JobStore, request: dict) -> dict:
    job = store.load(request.get("job_id"), request.get("experiment_id"))
    if container_state(job["container_id"])["Status"] not in ACTIVE_STATES:
        return status(store, request)
    (store.metadata_path(job["job_id"]).parent / "cancel_requested").touch()
    docker(["rm", "-f", job["container_id"]], check=False)
    job["status"] = "cancelled"
    store.save(job)
    return {"job_id": job["job_id"], "status": "cancelled"}


def export_operation(store: JobStore, request: dict, args) -> dict:
    """Only the Host invokes these operations; Gateway owns role/recipient checks."""
    exports = ReadonlyExports(store.root)
    actor = request.get("experiment_id")
    if not isinstance(actor, str) or not actor:
        raise PermissionError("Export is not accessible")
    operation = args.operation
    identifier = request.get("export_id")
    if operation == "exports-list":
        return {"exports": exports.list_accessible(actor)}
    if operation == "exports-check":
        ids = request.get("export_ids", [])
        if not isinstance(ids, list) or len(ids) > 64:
            raise ValueError("Expected an export ID list")
        for value in ids:
            exports.data_path(actor, value)
        return {"exports": [exports.read(actor, value) for value in ids]}
    if operation == "export-status":
        exports.read(actor, identifier)
        exports.refresh_sealer(identifier)
        return exports.read(actor, identifier)
    if operation == "export-manifest":
        return {"export_id": identifier, "files": exports.manifest(actor, identifier)}
    with (store.root / "locks/scheduler.lock").open("a") as scheduler:
        fcntl.flock(scheduler, fcntl.LOCK_EX)
        if operation == "export-create":
            job = store.load(request.get("job_id"), actor)
            location = request.get("location", "outputs")
            if location not in {"outputs", "workspace"}:
                raise ValueError("Expected outputs or workspace location")
            base = store.outputs(job) if location == "outputs" else store.workspace(job)
            source = safe_path(base, request.get("path"))
            record = exports.create(
                actor, job.get("run_id", actor), source, job["job_id"], location, request["path"]
            )
            if args.run_storage_bytes:
                planned = record["planned_bytes"]
                if store.run_storage_bytes(record["run_id"]) + planned >= args.run_storage_bytes:
                    exports.fail(record["export_id"], "run storage limit prevents export snapshot")
                    return exports.read(actor, record["export_id"])
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--seal-export",
                "--root",
                str(store.root),
                "--export-id",
                record["export_id"],
                "--storage-limit",
                str(args.run_storage_bytes),
            ]
            try:
                with (exports.records / f"{record['export_id']}.log").open("ab") as log:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                exports.set_pid(record["export_id"], process.pid)
            except OSError:
                exports.fail(record["export_id"], "Export sealer could not start")
                raise
            return exports.read(actor, record["export_id"])
        if operation == "export-grant":
            return exports.grant(actor, identifier, request.get("recipient"))
        if operation in {"export-revoke", "export-delete"}:
            mounted = active_jobs(store, input_export=identifier)
            if operation == "export-revoke":
                mounted = [
                    job for job in mounted if job["experiment_id"] == request.get("recipient")
                ]
            if mounted:
                raise ValueError(
                    "Export is mounted by active jobs; wait before revoking or deleting"
                )
            if operation == "export-revoke":
                return exports.revoke(actor, identifier, request.get("recipient"))
            return exports.delete(actor, identifier)
    raise ValueError("Unknown export operation")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--cpus", type=float, default=16)
    parser.add_argument("--memory", default="64g")
    parser.add_argument("--pids-limit", type=int, default=1024)
    parser.add_argument("--tmp-root", type=Path)
    parser.add_argument("--max-jobs", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--gpu-ids", default="")
    parser.add_argument("--share-gpus", action="store_true")
    parser.add_argument("--max-gpus", type=int, default=4)
    parser.add_argument("--gpu-memory-mb", type=int, default=512)
    parser.add_argument("--gpu-headroom-mb", type=int, default=2048)
    parser.add_argument("--gpu-capacity-mb", type=int, default=0)
    parser.add_argument("--run-storage-bytes", type=int, default=0)
    parser.add_argument("--models-root", type=Path)
    parser.add_argument("--preserve-models-path", action="store_true")
    parser.add_argument("--model-cache-root", type=Path)
    parser.add_argument("--datasets-root", type=Path)
    parser.add_argument(
        "operation",
        choices=[
            "submit",
            "status",
            "cancel",
            "artifact",
            "health",
            "usage",
            "export-create",
            "export-status",
            "export-manifest",
            "exports-list",
            "exports-check",
            "export-grant",
            "export-revoke",
            "export-delete",
        ],
    )
    args = parser.parse_args()
    if args.max_jobs < 0 or args.run_storage_bytes < 0 or args.timeout < 0:
        parser.error("max-jobs, storage bytes and timeout must be nonnegative")
    if (
        args.max_gpus < 1
        or args.gpu_memory_mb < 1
        or args.gpu_headroom_mb < 0
        or args.gpu_capacity_mb < 0
    ):
        parser.error("GPU count/reservation must be positive; headroom/capacity nonnegative")
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("request exceeds size limit")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise TypeError("request must be an object")
        store = JobStore(args.root)
        if args.operation.startswith("export"):
            print(json.dumps(export_operation(store, request, args)))
            return
        if args.operation == "usage":
            print(json.dumps(store.update_host_storage(request)))
            return
        if args.operation == "health":
            image = docker(["image", "inspect", "--format", "{{.Id}}", args.image]).stdout.strip()
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "protocol_version": 1,
                        "image": args.image,
                        "image_id": image,
                        "limits": {
                            "cpus": args.cpus,
                            "memory": args.memory,
                            "pids_limit": args.pids_limit,
                            "max_jobs": args.max_jobs,
                            "timeout_seconds": args.timeout,
                            "run_storage_bytes": args.run_storage_bytes,
                            "gpu_mode": "memory_shared" if args.share_gpus else "exclusive",
                            "max_gpus": args.max_gpus,
                            "gpu_memory_mb": args.gpu_memory_mb,
                            "gpu_headroom_mb": args.gpu_headroom_mb,
                            "gpu_capacity_mb": args.gpu_capacity_mb,
                        },
                        "assets": {
                            "models": "/models" if args.models_root else None,
                            "datasets": "/datasets" if args.datasets_root else None,
                            "model_cache": "/model-cache" if args.model_cache_root else None,
                        },
                    }
                )
            )
            return
        handlers = {"status": status, "cancel": cancel, "artifact": read_artifact}
        response = (
            submit(store, request, args)
            if args.operation == "submit"
            else handlers[args.operation](store, request)
        )
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        response = {"status": "error", "error": type(error).__name__, "message": str(error)}
    print(json.dumps(response))


if __name__ == "__main__":
    if "--seal-export" in sys.argv:
        sealer = argparse.ArgumentParser()
        sealer.add_argument("--seal-export", action="store_true")
        sealer.add_argument("--root", type=Path, required=True)
        sealer.add_argument("--export-id", required=True)
        sealer.add_argument("--storage-limit", type=int, default=0)
        args = sealer.parse_args()
        store = JobStore(args.root)
        exports = ReadonlyExports(args.root)
        record = exports.load(args.export_id)
        exports.set_pid(args.export_id, os.getpid())
        exports.seal(
            args.export_id, lambda: store.run_storage_bytes(record["run_id"]), args.storage_limit
        )
    elif "--watch" in sys.argv:
        watcher = argparse.ArgumentParser()
        watcher.add_argument("--watch", action="store_true")
        watcher.add_argument("--root", type=Path, required=True)
        watcher.add_argument("--job-id", required=True)
        watcher.add_argument("--timeout", type=int, required=True)
        watcher.add_argument("--lock-fd", type=int)
        watch(watcher.parse_args())
    else:
        main()
