"""Bounded workspace files, atomic metadata and storage accounting."""

import json
import os
import re
import uuid
from pathlib import Path, PurePosixPath

ACTIVE_STATES = {"created", "running", "restarting", "paused"}
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_LOG_BYTES = 8 * 1024 * 1024


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
    if set(request) - {
        "experiment_id",
        "run_id",
        "command",
        "gpu",
        "gpu_memory_mb",
        "files",
        "input_exports",
        "job_id",
        "retry_on_gpu_contention",
    }:
        raise ValueError("unknown submit fields")
    if "job_id" in request and (
        not isinstance(request["job_id"], str)
        or not re.fullmatch(r"[a-f0-9]{32}", request["job_id"])
    ):
        raise ValueError("invalid job_id")
    owner = request.get("experiment_id")
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", owner):
        raise ValueError("invalid experiment_id")
    run_id = request.get("run_id", owner)
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise ValueError("invalid run_id")
    command = request.get("command")
    if not isinstance(command, list) or not command or len(command) > 256:
        raise ValueError("command must be a nonempty argument list")
    if any(not isinstance(value, str) or "\x00" in value for value in command) or not command[0]:
        raise ValueError("command contains invalid arguments")
    if not isinstance(request.get("gpu"), bool):
        raise TypeError("gpu must be a boolean")
    if type(request.get("retry_on_gpu_contention", True)) is not bool:
        raise TypeError("retry_on_gpu_contention must be a boolean")
    memory = request.get("gpu_memory_mb")
    if memory is not None and (type(memory) is not int or memory < 1):
        raise ValueError("gpu_memory_mb must be a positive integer")
    if not isinstance(request.get("files", {}), dict):
        raise TypeError("files must be an object")
    inputs = request.get("input_exports", [])
    if (
        not isinstance(inputs, list)
        or len(inputs) > 64
        or any(not isinstance(x, str) for x in inputs)
        or len(set(inputs)) != len(inputs)
    ):
        raise ValueError("input_exports must contain at most 64 export IDs")


def job_timeout(seconds: int) -> int | None:
    if seconds < 0:
        raise ValueError("timeout must be nonnegative; zero means unlimited")
    return seconds or None


def directory_storage_bytes(root: Path) -> int:
    total = 0
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = [name for name in names if not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            try:
                if not path.is_symlink() and path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass  # A running job may replace or remove a file during the scan.
    return total


def public_job_status(job: dict) -> str:
    """Keep a failed attempt nonterminal while its watcher decides whether to retry."""
    if job.get("retry_pending") and job["status"] == "failed":
        return "restarting"
    return job["status"]
