"""Persist jobs and retain legacy and run-scoped workspace layouts."""

import json
import re
import time
import uuid
from pathlib import Path

from .files import atomic_json, directory_storage_bytes, public_job_status, write_files


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

    def list_jobs(self, request: dict) -> dict:
        """Page owned metadata without inspecting containers or changing jobs."""
        owner = request.get("experiment_id")
        if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", owner):
            raise PermissionError("invalid experiment_id")
        after = request.get("after", "")
        if after:
            self.metadata_path(after)
        limit = request.get("limit", 200)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        jobs = []
        for path in sorted((self.root / "jobs").glob("*/job.json")):
            if path.parent.name <= after:
                continue
            job = json.loads(path.read_text())
            if job["experiment_id"] != owner or job.get("run_id") != request.get("run_id"):
                continue
            jobs.append(
                {
                    "status": public_job_status(job),
                    **{
                        key: job.get(key)
                        for key in ("job_id", "created_at", "gpu", "gpu_uuid", "exit_code")
                    },
                }
            )
            if len(jobs) > limit:
                break
        return {
            "jobs": jobs[:limit],
            "next_after": jobs[limit - 1]["job_id"] if len(jobs) > limit else None,
        }

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
            if job.get("run_id") == run_id:
                total += directory_storage_bytes(metadata.parent / "attempts")
                archive = metadata.parent / "terminal-archive.json"
                if archive.exists():
                    total += archive.stat().st_size
                if job.get("temporary_directory"):
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
        job_id: str | None = None,
    ) -> dict:
        job = {
            "job_id": job_id or uuid.uuid4().hex,
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
