"""Admit trusted processes using whole-host headroom rather than per-job hard caps."""

import os
import shutil
from pathlib import Path


def sample(data_root: Path) -> dict:
    fields = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        fields[key] = int(value.split()[0])
    return {
        "cpus": len(os.sched_getaffinity(0)),
        "load": os.getloadavg()[0],
        "memory_available_mb": fields["MemAvailable"] / 1024,
        "disk_free_bytes": shutil.disk_usage(data_root).free,
    }


def admission(measurement: dict, args, jobs: list[dict]) -> dict | None:
    pending = sum(
        max(
            0, job.get("host_memory_mb", args.host_job_memory_mb) - job.get("observed_memory_mb", 0)
        )
        for job in jobs
        if job.get("backend") == "process"
    )
    reason = None
    if measurement["disk_free_bytes"] < args.disk_reserve_bytes:
        reason = "data_disk_headroom"
    elif measurement["memory_available_mb"] - pending < (
        args.host_memory_reserve_mb + args.host_job_memory_mb
    ):
        reason = "host_memory_pressure"
    elif max(
        measurement["load"],
        sum(
            job.get("host_cpus", args.host_job_cpus)
            for job in jobs
            if job.get("backend") == "process"
        ),
    ) + args.host_job_cpus > max(1, measurement["cpus"] - args.host_cpu_reserve):
        reason = "host_cpu_pressure"
    if reason:
        return {
            "job_id": None,
            "status": "resource_busy",
            "reason": reason,
            "retry_after_seconds": 5,
            "host_resources": measurement,
        }
    return None
