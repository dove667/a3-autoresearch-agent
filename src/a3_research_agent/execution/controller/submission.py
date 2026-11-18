"""Validate and submit jobs under the shared scheduler lock."""

import fcntl
import subprocess
import time
from pathlib import Path

from a3_research_agent.execution.exports.readonly import ReadonlyExports
from a3_research_agent.execution.scheduling import host_resources
from a3_research_agent.execution.scheduling.gpu import (
    acquire_gpu,
    active_jobs,
    record_scheduler_event,
    select_shared_gpu,
)

from . import processes
from .entry import controller_command
from .files import atomic_json, validate_submit
from .store import JobStore


def submit(store: JobStore, request: dict, args, *, queued_job: dict | None = None) -> dict:
    validate_submit(request)
    exports = ReadonlyExports(store.root)
    for identifier in request.get("input_exports", []):
        exports.data_path(request["experiment_id"], identifier)
    lease = None
    job = None
    with (store.root / "locks" / "scheduler.lock").open("a") as scheduler:
        fcntl.flock(scheduler, fcntl.LOCK_EX)
        limit = getattr(args, "run_storage_bytes", 0)
        run_id = request.get("run_id")
        if not run_id:
            raise ValueError("Process jobs require a Host-provided run_id")
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
        processes.validate_data_paths(
            Path(args.data_root), [store.root, Path(args.tmp_root or store.root / "temporary")]
        )
        jobs = active_jobs(store)
        busy = host_resources.admission(host_resources.sample(Path(args.data_root)), args, jobs)
        if busy:
            return busy
        if args.max_jobs and len(jobs) >= args.max_jobs:
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
                gpu_uuid = select_shared_gpu(store, args, jobs, memory_mb, diagnostics=allocation)
            else:
                allowed = set(args.gpu_ids.split(",")) if args.gpu_ids else None
                gpu_uuid, lease = acquire_gpu(store, allowed)
            if gpu_uuid is None:
                impossible = shared and memory_mb > allocation.get("max_capacity_mb", memory_mb)
                result = {
                    "job_id": None,
                    "status": "resource_unavailable" if impossible else "resource_busy",
                    "reason": "requested GPU memory exceeds allowed device capacity"
                    if impossible
                    else "no_eligible_gpu",
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
            if queued_job is not None:
                job = store.load(queued_job["job_id"], request["experiment_id"])
                if job["status"] != "queued":
                    return {"job_id": job["job_id"], "status": job["status"]}
                if job["command"] != request["command"] or job["run_id"] != run_id:
                    raise ValueError("Queued request does not match its durable job")
                job["status"] = "created"
                job["admitted_at"] = time.time()
            else:
                job = store.create(
                    request["experiment_id"],
                    request["command"],
                    request["gpu"],
                    request.get("files", {}),
                    run_id=run_id,
                    job_id=request.get("job_id"),
                )
            job["controller_options"] = {
                key: str(value) if isinstance(value, Path) else value
                for key, value in vars(args).items()
            }
            job["retry_on_gpu_contention"] = request.get("retry_on_gpu_contention", True)
            job["storage_limit_bytes"] = limit
            job["input_exports"] = request.get("input_exports", [])
            job["gpu_uuid"] = gpu_uuid
            job["gpu_shared"] = shared and request["gpu"]
            job["gpu_memory_mb"] = memory_mb if request["gpu"] else 0
            if allocation:
                job["gpu_allocation"] = allocation
            processes.prepare(store, job, args)
            store.save(job)
            if lease:
                atomic_json(
                    lease.path.with_suffix(".json"),
                    {
                        "job_id": job["job_id"],
                        "experiment_id": job["experiment_id"],
                    },
                )
            command = controller_command(
                "--watch",
                "--root",
                str(store.root),
                "--job-id",
                job["job_id"],
                "--timeout",
                str(args.timeout),
            )
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
            return {
                "job_id": job["job_id"],
                "status": "created",
                "backend": "process",
                "paths": job["paths"],
                "input_paths": job["input_paths"],
                "gpu_uuid": gpu_uuid,
                **({"gpu_allocation": allocation} if allocation else {}),
            }
        except Exception:
            if job is not None:
                job.update(
                    status="failed", error="Process job preparation or watcher launch failed"
                )
                store.save(job)
                processes.cleanup_temporary_directory(job)
            if lease:
                lease.path.with_suffix(".json").unlink(missing_ok=True)
            raise
        finally:
            if lease:
                lease.close()
