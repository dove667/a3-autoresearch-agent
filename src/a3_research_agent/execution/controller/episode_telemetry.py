"""Publish job-owned GPU usage without exposing host process identities."""

import csv
import math
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .files import atomic_json


def command_output(arguments: list[str]) -> str:
    return subprocess.run(arguments, capture_output=True, text=True, check=True, timeout=5).stdout


def nonnegative_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("GPU measurement must be finite and nonnegative")
    return number


def process_gpu_memory(device_arguments):
    """Include EGL graphics contexts; retain a conservative older-driver fallback."""
    try:
        root = ET.fromstring(command_output([*device_arguments, "-q", "-x"]))
        devices = root.findall("gpu")
        if len(devices) != 1 or devices[0].find("processes") is None:
            raise ValueError("GPU process table is unavailable")
        memory_by_pid = {}
        for row in devices[0].findall("processes/process_info"):
            pid = int(row.findtext("pid", ""))
            memory, unit = row.findtext("used_memory", "").split()
            if pid <= 0 or unit != "MiB":
                raise ValueError("Unsupported GPU process measurement")
            memory_by_pid[pid] = max(memory_by_pid.get(pid, 0), nonnegative_number(memory))
        return memory_by_pid, "owned_compute_and_graphics_plus_unattributed"
    except (ET.ParseError, ValueError, OSError, subprocess.SubprocessError):
        rows = csv.reader(
            command_output(
                [
                    *device_arguments,
                    "--query-compute-apps=pid,used_gpu_memory",
                    "--format=csv,noheader,nounits",
                ]
            ).splitlines(),
            skipinitialspace=True,
        )
        return {int(pid): nonnegative_number(memory) for pid, memory in rows}, (
            "conservative_graphics_upper_bound"
        )


def sample_gpu_resources(job: dict) -> dict:
    """Attribute compute and rendering memory to this job's host-visible processes."""
    sampled_at = time.time()
    if job.get("backend") == "process":
        from .processes import process_ids

        pids = set(process_ids(job))
        if not pids:
            raise ValueError("Job has no measurable processes")
    else:
        process_table = command_output(["docker", "top", job["container_id"], "-eo", "pid"])
        lines = process_table.splitlines()
        if not lines or lines[0].strip().upper() != "PID":
            raise ValueError("Container process table is unavailable")
        pids = {int(line.strip()) for line in lines[1:] if line.strip()}
        if not pids:
            raise ValueError("Container has no measurable processes")
    device_arguments = ["nvidia-smi", "-i", job["gpu_uuid"]]
    device_rows = list(
        csv.reader(
            command_output(
                [
                    *device_arguments,
                    "--query-gpu=memory.used,memory.total,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ]
            ).splitlines(),
            skipinitialspace=True,
        )
    )
    if len(device_rows) != 1 or len(device_rows[0]) != 3:
        raise ValueError("Assigned GPU measurement is unavailable")
    used, total, utilization = map(nonnegative_number, device_rows[0])
    if total <= 0:
        raise ValueError("Assigned GPU capacity is unavailable")
    process_memory, accounting = process_gpu_memory(device_arguments)
    owned_memory = sum(memory for pid, memory in process_memory.items() if pid in pids)
    # Keep unattributed driver overhead conservative without charging other jobs' EGL.
    budget_used = owned_memory + max(0, used - sum(process_memory.values()))
    return {
        "sampled_at": sampled_at,
        "gpu_used_mb": used,
        "gpu_total_mb": total,
        "gpu_util_percent": utilization,
        "gpu_budget_used_mb": budget_used,
        "gpu_limit_mb": job["gpu_memory_mb"],
        "gpu_accounting": accounting,
    }


class EpisodeTelemetry:
    """Sample in a bounded background loop; observation failures never stop the job."""

    def __init__(self, job: dict, interval_seconds: float = 2):
        self.job = dict(job)
        self.path = Path(job["episode_telemetry_directory"]) / "resources.json"
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="episode-telemetry", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join()

    def publish(self) -> None:
        try:
            sample = sample_gpu_resources(self.job)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            sample = {"sampled_at": time.time(), "error": "GPU measurement unavailable"}
        try:
            atomic_json(self.path, sample)
        except OSError:
            pass  # A stale prior sample also prevents admission in the consumer.

    def run(self) -> None:
        while not self.stop_event.is_set():
            self.publish()
            self.stop_event.wait(self.interval_seconds)
