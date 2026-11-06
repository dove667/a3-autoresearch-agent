"""Retry model initialization displaced by external GPU allocations."""

import base64
import fcntl
import json
import re
import time
from argparse import Namespace
from copy import copy
from pathlib import Path

from a3_research_agent.execution.scheduling.gpu import active_jobs, select_shared_gpu

from .docker import create_container
from .store import JobStore

MAX_CONTENTION_RETRIES = 2
OOM_MEMORY = re.compile(
    r"total capacity of ([\d.]+) (GiB|MiB) of which ([\d.]+) (GiB|MiB) is free.*?"
    r"Including non-PyTorch memory, this process has ([\d.]+) (GiB|MiB) memory in use",
    re.DOTALL,
)


def gpu_contention_evidence(job: dict, output: str) -> dict | None:
    """Require model-initialization frames and memory accounted outside this process."""
    if (
        "CUDA out of memory" not in output
        or "in from_pretrained" not in output
        or "in __init__" not in output
        or not re.search(r"\.to\([^\n]*device", output)
    ):
        return None
    match = OOM_MEMORY.search(output)
    if match is None:
        return None
    values = match.groups()
    total, free, owned = [
        float(values[index]) * (1024 if values[index + 1] == "GiB" else 1) for index in (0, 2, 4)
    ]
    budget = job.get("gpu_memory_mb", 0)
    other = total - free - owned
    # A full device alone cannot distinguish a model's own peak from competition.
    if not (0 <= free < total and 0 < owned < budget <= total and other > total - budget):
        return None
    return {"total_mb": total, "free_mb": free, "owned_mb": owned, "other_used_mb": other}


def prepare_retry(store: JobStore, previous: dict) -> bool:
    """Archive one failed attempt and re-admit the same logical job under fixed limits."""
    from .lifecycle import durable_json, lifecycle_lock

    directory = store.metadata_path(previous["job_id"]).parent
    with (store.root / "locks/scheduler.lock").open("a") as scheduler:
        fcntl.flock(scheduler, fcntl.LOCK_EX)
        with lifecycle_lock(store, previous):
            job = store.load(previous["job_id"], previous["experiment_id"])
            if (
                job["status"] != "failed"
                or job.get("error")
                or not job.get("gpu_shared")
                or not job.get("retry_on_gpu_contention", True)
                or (directory / "cancel_requested").exists()
            ):
                return False
            archive_path = directory / "terminal-archive.json"
            cleanup_path = directory / "cleanup.json"
            if (
                not archive_path.exists()
                or not cleanup_path.exists()
                or json.loads(cleanup_path.read_text()).get("status") != "removed"
            ):
                return False
            archive = json.loads(archive_path.read_text())
            output = "".join(
                base64.b64decode(archive[key]).decode(errors="replace")
                for key in ("stdout_base64", "stderr_base64")
            )
            evidence = gpu_contention_evidence(job, output)
            if evidence is None:
                return False
            if job.get("retry_count", 0) >= MAX_CONTENTION_RETRIES:
                job["retry"] = {"status": "exhausted", "reason": "retry_limit"}
                store.save(job)
                return False
            options = job.get("controller_options")
            if not options:
                return False
            args = Namespace(**options)
            jobs = active_jobs(store)
            allocation = {}
            gpu = None
            reason = "job_count_limit"
            if not args.max_jobs or len(jobs) < args.max_jobs:
                gpu = select_shared_gpu(
                    store, args, jobs, job["gpu_memory_mb"], diagnostics=allocation
                )
                reason = "no_eligible_gpu"
            if gpu and job.get("backend") == "process":
                from a3_research_agent.execution.scheduling import host_resources

                busy = host_resources.admission(
                    host_resources.sample(Path(args.data_root)), args, jobs
                )
                if busy:
                    gpu, reason = None, busy["reason"]
            if gpu is None:
                job["retry"] = {
                    "status": "resource_busy",
                    "reason": reason,
                    "retry_after_seconds": 2,
                    "gpu_allocation": allocation,
                    "evidence": evidence,
                }
                store.save(job)
                return False
            retry_record = {
                "retried_at": time.time(),
                "next_gpu_uuid": gpu,
                "attempt": job.get("retry_count", 0) + 2,
            }
            limit = job.get("storage_limit_bytes", 0)
            if (
                limit
                and store.run_storage_bytes(job["run_id"]) + len(json.dumps(retry_record).encode())
                > limit
            ):
                job["retry"] = {"status": "storage_limit", "reason": "run_storage_limit"}
                store.save(job)
                return False
            attempt = job.get("retry_count", 0) + 1
            attempt_directory = directory / "attempts" / str(attempt)
            attempt_directory.mkdir(parents=True, exist_ok=True)
            # Move the committed archive: it remains within the same storage budget.
            archive_path.replace(attempt_directory / "terminal-archive.json")
            cleanup_path.replace(attempt_directory / "cleanup.json")
            if job.get("backend") == "process":
                for name in ("stdout.log", "stderr.log"):
                    path = directory / name
                    if path.exists():
                        path.replace(attempt_directory / name)
                job.pop("process", None)
            durable_json(attempt_directory / "retry.json", retry_record)
            job.setdefault("attempts", []).append(
                {
                    "attempt": attempt,
                    "gpu_uuid": job["gpu_uuid"],
                    "exit_code": job.get("exit_code"),
                    "reason": "gpu_contention_during_loading",
                    "evidence": evidence,
                }
            )
            job.update(
                gpu_uuid=gpu,
                container_id=None,
                status="restarting",
                retry_count=attempt,
                retry={"status": "retrying", "reason": "gpu_contention_during_loading"},
                gpu_allocation=allocation,
            )
            for key in ("exit_code", "finished_at", "watcher_ended"):
                job.pop(key, None)
            store.save(job)
            telemetry = directory / "episode-telemetry/resources.json"
            telemetry.unlink(missing_ok=True)
            try:
                if job.get("backend") == "process":
                    from .processes import prepare

                    prepare(store, job, args)
                else:
                    job["container_id"] = create_container(store, job, args)
                job["status"] = "created"
                store.save(job)
            except Exception:
                job.update(
                    status="failed", error="container recreation failed; inspect before retry"
                )
                store.save(job)
                raise
            return True


def watch_with_retries(args, watch_attempt) -> None:
    """Run bounded attempts without resubmitting or extending the original time budget."""
    store = JobStore(args.root)
    job = json.loads(store.metadata_path(args.job_id).read_text())
    directory = store.metadata_path(args.job_id).parent
    started = time.monotonic()
    with (directory / "retry-supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            while True:
                job = store.load(args.job_id, job["experiment_id"])
                job["retry_pending"] = True
                store.save(job)
                remaining = copy(args)
                if args.timeout:
                    remaining.timeout = max(0.001, args.timeout - (time.monotonic() - started))
                watch_attempt(remaining)
                job = store.load(args.job_id, job["experiment_id"])
                if args.timeout and time.monotonic() - started >= args.timeout:
                    break
                prepared = prepare_retry(store, job)
                while not prepared and job.get("managed"):
                    job = store.load(args.job_id, job["experiment_id"])
                    if (
                        job.get("retry", {}).get("status") != "resource_busy"
                        or (directory / "cancel_requested").exists()
                        or args.timeout
                        and time.monotonic() - started >= args.timeout
                    ):
                        break
                    time.sleep(10)
                    prepared = prepare_retry(store, job)
                if not prepared:
                    break
        finally:
            from .lifecycle import lifecycle_lock

            with lifecycle_lock(store, job):
                job = store.load(args.job_id, job["experiment_id"])
                job.pop("retry_pending", None)
                if (directory / "cancel_requested").exists():
                    job["status"] = "cancelled"
                if job.get("retry", {}).get("status") == "retrying":
                    job["retry"]["status"] = job["status"]
                store.save(job)
