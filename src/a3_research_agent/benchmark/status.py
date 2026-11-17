"""Persist per-task benchmark states without deleting retired or solved tasks."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

STATES = ("open", "completed", "stable")
DEFAULT_PATH = Path("runs/checks/benchmark-task-status.json")


def signature(task) -> str:
    value = {
        "id": task.id,
        "question": task.question,
        "objective": task.objective,
        "route": task.route,
        "task": task.task,
        "criterion": task.criterion,
        "instructions": task.instructions,
        "require_review": task.require_review,
        "initial_status": task.initial_status,
        "initial_progress": task.initial_progress,
        "fixtures": list(task.fixtures),
    }
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load(tasks, path: Path = DEFAULT_PATH) -> dict:
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    entries = state.get("tasks") if isinstance(state, dict) else None
    if not isinstance(entries, dict):
        entries = {}
    for task in tasks:
        current = entries.get(task.id)
        expected = signature(task)
        if not isinstance(current, dict) or current.get("signature") != expected:
            entries[task.id] = {
                "state": "open",
                "passes": 0,
                "failures": 0,
                "signature": expected,
                "updated": None,
            }
    return {"version": 1, "tasks": entries}


def save(state: dict, path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def record(tasks, results, path: Path = DEFAULT_PATH, directory=None, model=None) -> dict:
    state = load(tasks, path)
    by_id = {task.id: task for task in tasks}
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    for row in results:
        task = by_id.get(row.get("id"))
        if task is None:
            continue
        entry = state["tasks"][task.id]
        entry["failures"] = int(entry.get("failures", 0)) + (0 if row.get("passed") else 1)
        if row.get("passed"):
            entry["passes"] = int(entry.get("passes", 0)) + 1
            entry["state"] = "stable" if entry["passes"] >= 2 else "completed"
        else:
            entry["state"] = "open"
        entry["updated"] = stamp
        entry["last_detail"] = str(row.get("detail", ""))[:300]
        if directory is not None:
            entry["last_directory"] = str(directory)
        if model:
            entry["last_model"] = model
    save(state, path)
    return state


def selected(tasks, state: str, only=None, path: Path = DEFAULT_PATH):
    if only:
        return [task for task in tasks if task.id in only]
    if state == "all":
        return list(tasks)
    current = load(tasks, path)["tasks"]
    return [task for task in tasks if current[task.id]["state"] == state]


def summary(tasks, path: Path = DEFAULT_PATH) -> dict:
    state = load(tasks, path)
    counts = {name: 0 for name in (*STATES, "total")}
    for task in tasks:
        counts[state["tasks"][task.id]["state"]] += 1
    counts["total"] = len(tasks)
    return counts
