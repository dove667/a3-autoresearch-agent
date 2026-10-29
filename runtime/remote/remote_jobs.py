"""Submit isolated container jobs using a host-controlled runtime directory."""

import argparse
import base64
import csv
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath

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
    if set(request) - {"experiment_id", "command", "gpu", "files"}:
        raise ValueError("unknown submit fields")
    owner = request.get("experiment_id")
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", owner):
        raise ValueError("invalid experiment_id")
    command = request.get("command")
    if not isinstance(command, list) or not command or len(command) > 256:
        raise ValueError("command must be a nonempty argument list")
    if any(not isinstance(value, str) or "\x00" in value for value in command) or not command[0]:
        raise ValueError("command contains invalid arguments")
    if not isinstance(request.get("gpu"), bool):
        raise TypeError("gpu must be a boolean")
    if not isinstance(request.get("files", {}), dict):
        raise TypeError("files must be an object")


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
        return self.root / "experiments" / job["experiment_id"] / job["job_id"]

    def create(self, experiment_id: str, command: list[str], gpu: bool, files: dict) -> dict:
        job = {
            "job_id": uuid.uuid4().hex,
            "experiment_id": experiment_id,
            "command": command,
            "gpu": gpu,
            "gpu_uuid": None,
            "status": "created",
            "created_at": time.time(),
            "container_id": None,
        }
        directory = self.metadata_path(job["job_id"]).parent
        directory.mkdir(mode=0o700)
        self.workspace(job).mkdir(parents=True, mode=0o700)
        write_files(self.workspace(job), files)
        self.save(job)
        return job


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


def create_container(store: JobStore, job: dict, args) -> str:
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
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=512m",
        "--log-driver",
        "local",
        "--log-opt",
        "max-size=1m",
        "--log-opt",
        "max-file=2",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--workdir",
        "/workspace",
        "--mount",
        f"type=bind,source={store.workspace(job)},target=/workspace",
    ]
    if job["gpu_uuid"]:
        arguments += ["--gpus", f"device={job['gpu_uuid']}"]
    return docker([*arguments, args.image, *job["command"]]).stdout.strip()


def count_active_jobs(store: JobStore) -> int:
    count = 0
    for path in (store.root / "jobs").glob("*/job.json"):
        job = json.loads(path.read_text())
        if job["container_id"] and container_state(job["container_id"])["Status"] in ACTIVE_STATES:
            count += 1
    return count


def submit(store: JobStore, request: dict, args) -> dict:
    validate_submit(request)
    lease = None
    container_id = None
    with (store.root / "locks" / "scheduler.lock").open("a") as scheduler:
        fcntl.flock(scheduler, fcntl.LOCK_EX)
        if count_active_jobs(store) >= args.max_jobs:
            return {"job_id": None, "status": "resource_busy", "retry_after_seconds": 2}
        gpu_uuid = None
        if request["gpu"]:
            allowed = set(args.gpu_ids.split(",")) if args.gpu_ids else None
            gpu_uuid, lease = acquire_gpu(store, allowed)
            if lease is None:
                return {"job_id": None, "status": "resource_busy", "retry_after_seconds": 2}
        try:
            job = store.create(
                request["experiment_id"],
                request["command"],
                request["gpu"],
                request.get("files", {}),
            )
            job["gpu_uuid"] = gpu_uuid
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
            return {"job_id": job["job_id"], "status": "created", "gpu_uuid": gpu_uuid}
        except Exception:
            if container_id:
                docker(["rm", "-f", container_id], check=False)
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
        result = subprocess.run(
            ["docker", "wait", job["container_id"]],
            capture_output=True,
            text=True,
            check=True,
            timeout=args.timeout,
        )
        job["exit_code"] = int(result.stdout.strip())
        job["status"] = "completed" if job["exit_code"] == 0 else "failed"
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--cpus", type=float, default=2)
    parser.add_argument("--memory", default="8g")
    parser.add_argument("--pids-limit", type=int, default=256)
    parser.add_argument("--max-jobs", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--gpu-ids", default="")
    parser.add_argument("operation", choices=["submit", "status", "cancel", "artifact", "health"])
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("request exceeds size limit")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise TypeError("request must be an object")
        store = JobStore(args.root)
        if args.operation == "health":
            image = docker(["image", "inspect", "--format", "{{.Id}}", args.image]).stdout.strip()
            print(
                json.dumps(
                    {"status": "ok", "protocol_version": 1, "image": args.image, "image_id": image}
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
    if "--watch" in sys.argv:
        watcher = argparse.ArgumentParser()
        watcher.add_argument("--watch", action="store_true")
        watcher.add_argument("--root", type=Path, required=True)
        watcher.add_argument("--job-id", required=True)
        watcher.add_argument("--timeout", type=int, required=True)
        watcher.add_argument("--lock-fd", type=int)
        watch(watcher.parse_args())
    else:
        main()
