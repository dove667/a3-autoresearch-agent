"""Durable server-owned admission queue; scheduling never calls a research model."""

import fcntl
import json
import os
import subprocess
import time
from argparse import Namespace
from pathlib import Path

from .entry import controller_command
from .files import ACTIVE_STATES, atomic_json, public_job_status, validate_submit
from .store import JobStore


def queue_path(store):
    return store.root / "managed-jobs.json"


def load_index(store):
    path = queue_path(store)
    return json.loads(path.read_text()) if path.exists() else []


def ensure_dispatcher(store):
    from .processes import is_running

    with (store.root / "locks/dispatcher-start.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        identity = store.root / "dispatcher.json"
        if identity.exists() and is_running({"process": json.loads(identity.read_text())}):
            return
        with (store.root / "dispatcher.log").open("ab") as output:
            subprocess.Popen(
                controller_command("--dispatch", "--root", str(store.root)),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )


def enqueue(store: JobStore, request: dict, args) -> dict:
    from .processes import command, prepare, validate_data_paths

    validate_submit(request)
    if args.backend != "process":
        raise ValueError("Managed queue requires the trusted process backend")
    validate_data_paths(
        Path(args.data_root), [store.root, Path(args.tmp_root or store.root / "temporary")]
    )
    with (store.root / "locks/scheduler.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        run_id = request.get("run_id")
        if not run_id:
            raise ValueError("Managed jobs require a run ID")
        limit = args.run_storage_bytes
        uploaded = sum(len(value.encode()) for value in request.get("files", {}).values())
        if limit and store.run_storage_bytes(run_id) + uploaded >= limit:
            return {"job_id": None, "status": "storage_limit"}
        job = store.create(
            request["experiment_id"],
            request["command"],
            request["gpu"],
            request.get("files", {}),
            run_id=run_id,
            job_id=request.get("job_id"),
        )
        try:
            job.update(
                backend="process",
                status="queued",
                managed=True,
                input_exports=request.get("input_exports", []),
                storage_limit_bytes=limit,
                retry_on_gpu_contention=request.get("retry_on_gpu_contention", True),
                controller_options={
                    k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()
                },
                queued_request={k: v for k, v in request.items() if k != "files"},
            )
            prepare(store, job, args)
            command(job)
            store.save(job)
            identifiers = load_index(store)
            identifiers.append(job["job_id"])
            atomic_json(queue_path(store), identifiers)
        except Exception:
            job.update(status="failed", error="Queued job preparation failed")
            store.save(job)
            raise
    try:
        ensure_dispatcher(store)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        current = store.load(job["job_id"], job["experiment_id"])
        current["controller_incident"] = {"type": type(error).__name__, "time": time.time()}
        store.save(current)
    return {
        "job_id": job["job_id"],
        "status": "queued",
        "backend": "process",
        "paths": job["paths"],
        "input_paths": job["input_paths"],
    }


def automatic_gpu_budget(store, request, args):
    """Assign available capacity; optional legacy memory values are minimum hints."""
    from a3_research_agent.execution.scheduling.gpu import active_jobs, select_shared_gpu

    if not request["gpu"] or not args.share_gpus:
        return request
    minimum = min(request.get("gpu_memory_mb") or 8192, args.gpu_capacity_mb or 8192)
    report = {}
    gpu = select_shared_gpu(store, args, active_jobs(store), minimum, diagnostics=report)
    if gpu is None:
        return None
    device = next(row for row in report["devices"] if row["uuid"] == gpu)
    return {**request, "gpu_memory_mb": max(minimum, int(device["available_mb"]))}


def dispatch_once(store):
    """Admit independent ready jobs; leave resource pressure in the server queue."""
    from .processes import is_running
    from .submission import submit

    identifiers = load_index(store)
    for identifier in identifiers:
        job = json.loads(store.metadata_path(identifier).read_text())
        if (
            job["status"] in ACTIVE_STATES
            and time.time() - job.get("admitted_at", job["created_at"]) > 30
        ):
            watcher = job.get("watcher")
            if not is_running({"process": watcher}) and not is_running(job):
                job.update(
                    status="failed",
                    error="Execution stopped; watcher unavailable",
                    finished_at=time.time(),
                )
                job.pop("retry_pending", None)
                store.save(job)
        if job["status"] != "queued":
            continue
        args = Namespace(**job["controller_options"])
        try:
            with (store.root / "locks/scheduler.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                request = automatic_gpu_budget(store, job["queued_request"], args)
            if request is None:
                continue
            # submit rechecks every limit under the scheduler lock before launching.
            result = submit(store, request, args, queued_job=job)
            if result["status"] in {"resource_busy", "resource_unavailable", "storage_limit"}:
                continue
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            # Persist infrastructure incidents for the human operator. Do not ask the model
            # to diagnose the scheduler, and never replay an uncertain launch.
            current = store.load(identifier, job["experiment_id"])
            current["controller_incident"] = {"type": type(error).__name__, "time": time.time()}
            store.save(current)
    with (store.root / "locks/scheduler.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Include concurrently enqueued jobs; only remove durable terminal records.
        active = [
            identifier
            for identifier in load_index(store)
            if public_job_status(json.loads(store.metadata_path(identifier).read_text()))
            not in {"completed", "failed", "cancelled"}
        ]
        atomic_json(queue_path(store), active)
    return len(active)


def run_dispatcher(root):
    from .processes import boot_id, read_process

    store = JobStore(root)
    with (store.root / "locks/dispatcher.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        identity = {
            "pid": os.getpid(),
            "start_ticks": read_process(os.getpid())["start_ticks"],
            "boot_id": boot_id(),
        }
        atomic_json(store.root / "dispatcher.json", identity)
        idle_since = time.monotonic()
        while True:
            try:
                if dispatch_once(store):
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since >= 60:
                    with (store.root / "locks/dispatcher-start.lock").open("a") as start_lock:
                        fcntl.flock(start_lock, fcntl.LOCK_EX)
                        if not load_index(store):
                            (store.root / "dispatcher.json").unlink(missing_ok=True)
                            return
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                print(f"Controller observation will retry: {type(error).__name__}", flush=True)
            time.sleep(10)
