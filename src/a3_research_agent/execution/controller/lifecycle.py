"""Coordinate watcher attempts and durable job records."""

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from .store import JobStore


@contextmanager
def lifecycle_lock(store: JobStore, job: dict):
    with (store.metadata_path(job["job_id"]).parent / "lifecycle.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def durable_json(path: Path, value: dict) -> None:
    """Commit JSON bytes and the directory entry to disk."""
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


def watch(args) -> None:
    from .processes import watch_attempt

    job = JobStore(args.root).load_process_job(args.job_id)
    if job.get("gpu_shared") and job.get("retry_on_gpu_contention", False):
        from .retry import watch_with_retries

        watch_with_retries(args, watch_attempt)
    else:
        watch_attempt(args)
