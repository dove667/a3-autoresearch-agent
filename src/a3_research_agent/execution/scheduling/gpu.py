"""Coordinate GPU leases and explain shared-memory admission decisions."""

import csv
import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

from a3_research_agent.execution.controller.docker import container_state, container_states, docker
from a3_research_agent.execution.controller.files import ACTIVE_STATES
from a3_research_agent.execution.controller.store import JobStore


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


def active_jobs(store: JobStore, *, input_export: str | None = None) -> list[dict]:
    tracked = []
    for path in (store.root / "jobs").glob("*/job.json"):
        job = json.loads(path.read_text())
        if input_export is not None and input_export not in job.get("input_exports", []):
            continue
        cleanup = path.with_name("cleanup.json")
        if cleanup.exists() and json.loads(cleanup.read_text()).get("status") == "removed":
            continue
        if job["container_id"]:
            tracked.append(job)
    states = container_states([job["container_id"] for job in tracked])
    return [job for job in tracked if states[job["container_id"]]["Status"] in ACTIVE_STATES]


def count_active_jobs(store: JobStore) -> int:
    return len(active_jobs(store))
