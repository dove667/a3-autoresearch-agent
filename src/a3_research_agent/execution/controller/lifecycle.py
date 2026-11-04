"""Archive terminal jobs before cleanup and supervise container lifecycle."""

import base64
import fcntl
import hashlib
import json
import os
import selectors
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .docker import cleanup_temporary_directory, container_state, docker
from .files import ACTIVE_STATES, MAX_ARCHIVE_LOG_BYTES, atomic_json, job_timeout
from .store import JobStore


@contextmanager
def lifecycle_lock(store: JobStore, job: dict):
    with (store.metadata_path(job["job_id"]).parent / "lifecycle.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def durable_json(path: Path, value: dict) -> None:
    """Commit archive bytes and directory entry before a destructive Docker operation."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        with temporary.open("w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def owned_container(store: JobStore, job: dict) -> dict:
    result = docker(["inspect", job["container_id"]])
    records = json.loads(result.stdout)
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise RuntimeError("container inspection is malformed")
    info = records[0]
    if not isinstance(info.get("State"), dict) or not isinstance(info.get("Config"), dict):
        raise TypeError("container inspection is incomplete")
    labels = info["Config"].get("Labels") or {}
    if not isinstance(labels, dict):
        raise TypeError("container labels are malformed")
    if (
        info.get("Id") != job["container_id"]
        or info.get("Name") != f"/a3-job-{job['job_id']}"
        or labels.get("a3.root") != hashlib.sha256(str(store.root).encode()).hexdigest()
        or labels.get("a3.experiment") != job["experiment_id"]
        or labels.get("a3.job", job["job_id"]) != job["job_id"]
    ):
        raise RuntimeError("container ownership could not be verified")
    return info


def capture_container_logs(container_id: str) -> tuple[bytes, bytes]:
    """Read all retained Docker logs, or fail without deleting on timeout/size limit."""
    chunks = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    deadline = time.monotonic() + 30
    with subprocess.Popen(
        ["docker", "logs", container_id], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError("Docker log archival timed out")
                    for key, _ in selector.select(remaining):
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(data)
                        if total > MAX_ARCHIVE_LOG_BYTES:
                            raise RuntimeError(
                                "available logs exceed 8 MiB archive safety limit; retained"
                            )
                        chunks[key.data].extend(data)
            if process.wait(timeout=max(0.01, deadline - time.monotonic())):
                raise RuntimeError("Docker logs could not be archived")
        except BaseException:
            process.kill()  # Only the read-only docker logs client, never the job container.
            process.wait()
            raise
    return bytes(chunks["stdout"]), bytes(chunks["stderr"])


def cancelled_before_start(store: JobStore, job: dict, state: dict) -> bool:
    return (
        state.get("Status") == "created"
        and job.get("status") == "cancelled"
        and (store.metadata_path(job["job_id"]).parent / "cancel_requested").exists()
    )


def reclaim_terminal(store: JobStore, job: dict) -> dict:
    """Caller holds lifecycle lock. No force removal; failed cleanup is retryable."""
    directory = store.metadata_path(job["job_id"]).parent
    archive_path = directory / "terminal-archive.json"
    cleanup = {"status": "retained"}
    try:
        state = container_state(job["container_id"])
        if state["Status"] == "removed":
            cleanup = {"status": "removed" if archive_path.exists() else "missing_unarchived"}
        elif job.get("status") not in {"completed", "failed", "cancelled"}:
            cleanup = {
                "status": "retained",
                "reason": "watcher has not committed a terminal job record",
            }
        elif state["Status"] not in {"exited", "dead"} and not cancelled_before_start(
            store, job, state
        ):
            cleanup = {"status": "retained", "reason": "container is not fully terminal"}
        else:
            info = owned_container(store, job)
            if info["State"]["Status"] not in {"exited", "dead"} and not cancelled_before_start(
                store, job, info["State"]
            ):
                raise RuntimeError("container changed state before archival")
            stdout, stderr = capture_container_logs(job["container_id"])
            archive = {
                "version": 1,
                "archived_at": time.time(),
                "job": job,
                "container": info,
                "log_scope": "all currently available Docker logs; rotated logs unavailable",
                "stdout_base64": base64.b64encode(stdout).decode(),
                "stderr_base64": base64.b64encode(stderr).decode(),
            }
            encoded_size = len(json.dumps(archive).encode())
            existing_size = archive_path.stat().st_size if archive_path.exists() else 0
            if job.get("storage_limit_bytes") and (
                store.run_storage_bytes(job["run_id"]) - existing_size + encoded_size
                > job["storage_limit_bytes"]
            ):
                raise RuntimeError("run storage limit prevents durable log archive")
            durable_json(archive_path, archive)
            if json.loads(archive_path.read_text()) != archive:
                raise RuntimeError("archive verification failed")
            # Re-inspect under our lock. Non-force rm also rejects an external restart race.
            latest = owned_container(store, job)
            if latest["State"] != info["State"]:
                raise RuntimeError("container state changed after archival")
            docker(["rm", job["container_id"]])
            cleanup = {"status": "removed", "archived_at": archive["archived_at"]}
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        cleanup = {"status": "retained", "reason": str(error)}
    cleanup["checked_at"] = time.time()
    try:
        atomic_json(directory / "cleanup.json", cleanup)
    except OSError:
        pass  # Return the failure even when the metadata volume cannot accept writes.
    return cleanup


def watch(args) -> None:
    store = JobStore(args.root)
    job = json.loads(store.metadata_path(args.job_id).read_text())
    cancel_path = store.metadata_path(args.job_id).parent / "cancel_requested"
    try:
        with lifecycle_lock(store, job):
            if cancel_path.exists():
                raise RuntimeError("job cancelled before start")
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
        with lifecycle_lock(store, job):
            if cancel_path.exists():
                job["status"] = "cancelled"
            state = container_state(job["container_id"])
            if state["Status"] in ACTIVE_STATES and not cancelled_before_start(store, job, state):
                try:
                    # Preserve watcher failure termination, separately from terminal GC.
                    owned_container(store, job)
                    docker(["stop", "--time", "10", job["container_id"]])
                    if container_state(job["container_id"])["Status"] in ACTIVE_STATES:
                        docker(["kill", job["container_id"]])
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    RuntimeError,
                    subprocess.SubprocessError,
                ) as error:
                    job["error"] = (
                        f"{job.get('error', 'watcher ended')}; termination failed: {error}"
                    )
                state = container_state(job["container_id"])
            job["watcher_ended"] = True
            if state["Status"] in ACTIVE_STATES and not cancelled_before_start(store, job, state):
                job["status"] = state["Status"]
                job.pop("finished_at", None)
            else:
                job["finished_at"] = time.time()
            store.save(job)
            reclaim_terminal(store, job)
        if container_state(job["container_id"])["Status"] not in ACTIVE_STATES:
            cleanup_temporary_directory(job)
        if args.lock_fd is not None:
            lease_path = store.root / "locks" / f"{job['gpu_uuid']}.json"
            if container_state(job["container_id"])["Status"] not in ACTIVE_STATES:
                lease_path.unlink(missing_ok=True)
            os.close(args.lock_fd)


def state_for_status(container_id: str) -> dict:
    try:
        return container_state(container_id)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return {"Status": "unknown"}


def status(store: JobStore, request: dict) -> dict:
    job = store.load(request.get("job_id"), request.get("experiment_id"))
    with lifecycle_lock(store, job):
        job = store.load(job["job_id"], job["experiment_id"])
        state = state_for_status(job["container_id"])
        if (
            job.get("watcher_ended")
            and job["status"] in ACTIVE_STATES
            and state["Status"] in {"exited", "dead"}
        ):
            cancelled = (store.metadata_path(job["job_id"]).parent / "cancel_requested").exists()
            job["status"] = "cancelled" if cancelled else "failed"
            job["exit_code"] = state.get("ExitCode")
            job["finished_at"] = time.time()
            store.save(job)
        cleanup = reclaim_terminal(store, job)
        state = state_for_status(job["container_id"])
        archive_path = store.metadata_path(job["job_id"]).parent / "terminal-archive.json"
        if state["Status"] == "removed" and archive_path.exists():
            archive = json.loads(archive_path.read_text())
            state = archive["container"]["State"]
            output = (
                base64.b64decode(archive["stdout_base64"])
                + base64.b64decode(archive["stderr_base64"])
            ).decode("utf-8", errors="replace")
        elif state["Status"] not in {"removed", "unknown"}:
            logs = docker(["logs", "--tail", "200", job["container_id"]], check=False)
            output = logs.stdout + logs.stderr if not logs.returncode else ""
        else:
            output = ""
        current = state["Status"]
        if current == "unknown":
            current = job["status"]
        elif (
            cleanup["status"] == "removed"
            or current not in ACTIVE_STATES
            or cancelled_before_start(store, job, state)
        ):
            current = job["status"]
            if current in ACTIVE_STATES:
                current = "completed" if state.get("ExitCode") == 0 else "failed"
        return {
            "job_id": job["job_id"],
            "status": current,
            "gpu_uuid": job["gpu_uuid"],
            "gpu_shared": job.get("gpu_shared", False),
            "gpu_memory_mb": job.get("gpu_memory_mb"),
            "exit_code": state.get("ExitCode", job.get("exit_code")),
            "output": output[-65536:],
            "cleanup": cleanup,
            **({"error": job["error"]} if "error" in job else {}),
        }


def cancel(store: JobStore, request: dict) -> dict:
    job = store.load(request.get("job_id"), request.get("experiment_id"))
    with lifecycle_lock(store, job):
        state = container_state(job["container_id"])
        if state["Status"] in ACTIVE_STATES:
            owned_container(store, job)
            (store.metadata_path(job["job_id"]).parent / "cancel_requested").touch()
            if state["Status"] != "created":
                docker(["stop", "--time", "10", job["container_id"]])
                if container_state(job["container_id"])["Status"] in ACTIVE_STATES:
                    raise RuntimeError("cancel did not stop container; retained")
            job.update(status="cancelled", finished_at=time.time())
            store.save(job)
        reclaim_terminal(store, job)
    return status(store, request)
