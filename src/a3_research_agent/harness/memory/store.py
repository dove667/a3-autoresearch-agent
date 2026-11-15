"""Append immutable note revisions under one process-safe project lock."""

import fcntl
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from a3_research_agent.harness import jsonl_store

_LOCK = threading.RLock()


def digest(record):
    body = {key: value for key, value in record.items() if key != "sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def checked_root(root):
    root = Path(root)
    if not root.is_absolute():
        raise ValueError("Memory root must be absolute")
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("Memory storage cannot use symbolic links")
    root.mkdir(parents=True, exist_ok=True)
    for name in ("entries.jsonl", ".lock", "index.sqlite3"):
        if (root / name).is_symlink():
            raise ValueError("Memory storage cannot use symbolic links")
    return root


@contextmanager
def locked(root):
    root = checked_root(root)
    with _LOCK, (root / ".lock").open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield root
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def load_events(root):
    return [
        row
        for row in jsonl_store.load_records(Path(root) / "entries.jsonl")
        if isinstance(row.get("id"), str) and isinstance(row.get("text"), str)
    ]


def current(root):
    latest = {}
    for event in load_events(root):
        row = {
            "revision": 1,
            "kind": "note",
            "scope": "",
            "refs": [],
            "supersedes": "",
            "status": "active",
            "lifetime": "durable",
            "run_ids": [],
            "created_run": "",
            "tags": [],
            **event,
        }
        row["sha256"] = digest(row)
        latest[row["id"]] = row
    return latest


def append(root, event):
    event = {**event, "sha256": digest(event)}
    target = Path(root) / "entries.jsonl"
    existed = target.exists()
    jsonl_store.append_record(target, event)
    if not existed:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return event


def applicable(entry, run_id):
    return entry["lifetime"] == "durable" or run_id in entry["run_ids"]


def superseded_by(entries, run_id=None):
    result = {}
    for entry in entries.values():
        if run_id is not None and not applicable(entry, run_id):
            continue
        previous = entry.get("supersedes")
        if previous:
            result.setdefault(previous, []).append(entry["id"])
    return result
