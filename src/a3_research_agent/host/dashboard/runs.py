"""Observe research runs without owning or modifying them."""

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .readers import mtime, newest_mtime, read_json, read_json_lines

RUN_ID = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{6}")
TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled"})

STATE_LABELS = {
    "not_started": ("未启动", "idle"),
    "starting": ("启动中", "active"),
    "running": ("运行中", "active"),
    "waiting_jobs": ("等待远程作业", "wait"),
    "waiting_event": ("等待事件", "wait"),
    "context_waiting": ("整理上下文", "wait"),
    "context_blocked": ("上下文受阻", "alert"),
    "blocked": ("已阻塞", "alert"),
    "suspended": ("已暂停", "paused"),
    "stopped": ("已停止", "idle"),
    "interrupted": ("异常中断", "alert"),
    "unknown": ("状态未知", "idle"),
}

JOB_LABELS = {
    "queued": ("排队中", "wait"),
    "running": ("运行中", "active"),
    "completed": ("已完成", "done"),
    "failed": ("失败", "alert"),
    "cancelled": ("已取消", "idle"),
    "unobserved": ("未观察", "idle"),
}


def run_ids(project_root):
    root = Path(project_root) / "trajectories"
    try:
        names = [path.name for path in root.iterdir() if path.is_dir()]
    except OSError:
        return []
    return sorted((name for name in names if RUN_ID.fullmatch(name)), reverse=True)


def latest_run_id(project_root):
    root = Path(project_root) / "trajectories"
    try:
        pointer = (root / "latest-run.txt").read_text(encoding="utf-8").strip()
    except OSError:
        pointer = ""
    names = run_ids(project_root)
    pointer_name = Path(pointer).name if pointer else ""
    if pointer_name in names:
        return pointer_name
    return names[0] if names else None


def select_run(project_root, selector="latest"):
    """Resolve a run id or a directory below this project's trajectories root."""
    root = (Path(project_root) / "trajectories").resolve()
    if not selector or selector == "latest":
        return latest_run_id(project_root)
    if RUN_ID.fullmatch(selector):
        candidate = root / selector
    else:
        try:
            candidate = Path(selector).resolve()
        except OSError:
            return None
        if candidate.parent != root:
            return None
    return candidate.name if candidate.is_dir() else None


def process_alive(pid, started):
    """Compare a saved process identity with the live process table."""
    if type(pid) is not int or pid <= 0:
        return False
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True,
        text=True,
        env={"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"},
        check=False,
        timeout=5,
    )
    if result.returncode == 1:
        return False
    if result.returncode or not result.stdout.strip():
        return None
    observed = result.stdout.strip()
    return observed == started if started else bool(observed)


def _started_at(run, startup):
    value = (startup.get("input_manifest") or {}).get("time")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            pass
    try:
        return datetime.strptime(run.name, "%Y-%m-%d_%H-%M-%S-%f").timestamp()
    except ValueError:
        return mtime(run) or time.time()


def _sessions(run, process_observer):
    supervised = {}
    supervision = read_json(run / "supervision.json", {}) or {}
    for item in supervision.get("sessions") or []:
        if isinstance(item, dict) and isinstance(item.get("session_id"), str):
            supervised[item["session_id"]] = item
    result = []
    for path in sorted((run / "sessions").glob("*/session.json")):
        record = read_json(path, {}) or {}
        session_id = record.get("session_id") or path.parent.name
        process = record.get("process") or {}
        pid, started = process.get("pid"), process.get("started")
        alive = process_observer(pid, started)
        item = supervised.get(session_id, {})
        if record.get("closed"):
            status = "closed"
        elif alive is True:
            status = "running"
        elif alive is False:
            status = "exited"
        else:
            status = "unknown"
        result.append(
            {
                "id": session_id,
                "status": status,
                "alive": alive,
                "closed": bool(record.get("closed")),
                "pid": pid,
                "started": started,
                "exit_code": item.get("exit_code"),
            }
        )
    return result


def _pending_call(run):
    value = read_json(run / "conversation/pending-calls.json", None)
    if not isinstance(value, dict) or not isinstance(value.get("output"), list):
        return {"names": [], "jobs": [], "since": None}
    names, jobs = [], []
    for item in value["output"]:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        name = item.get("name")
        if isinstance(name, str):
            names.append(name)
        if name == "job_wait":
            try:
                arguments = item.get("arguments")
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if isinstance(arguments, dict):
                    jobs.extend(
                        job for job in arguments.get("job_ids") or [] if isinstance(job, str)
                    )
            except ValueError:
                pass
    return {
        "names": names,
        "jobs": list(dict.fromkeys(jobs)),
        "since": mtime(run / "conversation/pending-calls.json"),
    }


def _decision_times(run):
    rows = read_json_lines(
        run / "conversation/trajectory.jsonl",
        max_bytes=4_000_000,
    )
    result = []
    for row in rows:
        if "response" not in row:
            continue
        response = row.get("response")
        if not isinstance(response, dict):
            continue
        value = response.get("created_at")
        if isinstance(value, (int, float)):
            result.append(float(value))
    return sorted(result)


def _usage(run, startup):
    rows = read_json_lines(run / "model-usage.jsonl", max_bytes=2_000_000)
    last = rows[-1] if rows else None
    used = None
    if isinstance(last, dict):
        used = last.get("estimated_input_tokens")
        if not isinstance(used, (int, float)):
            usage = last.get("usage")
            used = usage.get("input_tokens") if isinstance(usage, dict) else None
    budget = startup.get("context_budget") or {}
    limit = budget.get("main_context_tokens")
    percent = None
    if isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
        percent = round(min(100, used / limit * 100))
    return {
        "calls": len(rows),
        "used_tokens": int(used) if isinstance(used, (int, float)) else None,
        "limit_tokens": int(limit) if isinstance(limit, (int, float)) else None,
        "percent": percent,
    }


def _job_inputs(run):
    """Read the Host's own submission receipt for each job."""
    result = {}
    for row in read_json_lines(run / "job-inputs.jsonl", max_bytes=4_000_000):
        job = row.get("job_id")
        if not isinstance(job, str):
            continue
        command = row.get("command")
        source_files = []
        for item in row.get("source_files") or []:
            if isinstance(item, dict):
                path = item.get("path") or item.get("destination")
                if isinstance(path, str):
                    source_files.append(path)
            elif isinstance(item, str):
                source_files.append(item)
        result[job] = {
            "name": row.get("name") if isinstance(row.get("name"), str) else "",
            "command": [part for part in command if isinstance(part, str)]
            if isinstance(command, list)
            else [],
            "source_files": list(dict.fromkeys(source_files)),
            "submitted_at": row.get("submitted_at")
            if isinstance(row.get("submitted_at"), (int, float))
            else None,
            "gpu": bool(row.get("gpu", False)),
        }
    return result


def _jobs(run):
    state = read_json(run / "conversation/operational-state.json", {}) or {}
    observed = state.get("jobs") if isinstance(state.get("jobs"), dict) else {}
    submitted = _job_inputs(run)
    result = {}
    for job in sorted(set(observed) | set(submitted)):
        if not isinstance(job, str):
            continue
        row = observed.get(job) if isinstance(observed.get(job), dict) else {}
        receipt = submitted.get(job, {})
        result[job] = {
            "status": row.get("status", "unobserved"),
            "observed_at": row.get("observed_at"),
            "submitted_at": receipt.get("submitted_at"),
            "name": receipt.get("name", ""),
            "command": receipt.get("command", []),
            "source_files": receipt.get("source_files", []),
            "gpu": receipt.get("gpu", False),
        }
    return result


def _state(run, host, supervision, outcome, context_failure, sessions, pending):
    if run is None:
        return "not_started"
    phase = supervision.get("phase")
    host_status = host.get("status")
    blocked = outcome.get("status") == "blocked"
    context_status = context_failure.get("status")
    alive = [session["alive"] for session in sessions]
    process = True if True in alive else False if False in alive else None

    if blocked:
        return "blocked"
    if phase == "stopped":
        return "stopped"
    if host_status == "suspended":
        return "suspended"
    if host_status in {"starting", "created"} and process is not False:
        return "starting"
    if process is True:
        if context_status == "blocked":
            return "context_blocked"
        if context_status == "waiting":
            return "context_waiting"
        if "job_wait" in pending["names"]:
            return "waiting_jobs"
        if "idle_wait" in pending["names"]:
            return "waiting_event"
        return "running"
    if process is False:
        if host_status in {"running", "starting"} or phase == "supervising":
            return "interrupted"
        return "stopped"
    if host_status == "running" or phase == "supervising":
        return "interrupted"
    return "unknown"


def read_run(run, *, process_observer=process_alive):
    """Read one run's observable state; the dashboard never writes to it."""
    run = Path(run)
    startup = read_json(run / "startup.json", {}) or {}
    host = read_json(run / ".host/state.json", {}) or {}
    supervision = read_json(run / "supervision.json", {}) or {}
    outcome = read_json(run / "conversation/run-outcome.json", {}) or {}
    context_failure = read_json(run / "conversation/context-failure.json", {}) or {}
    sessions = _sessions(run, process_observer)
    pending = _pending_call(run)
    decisions = _decision_times(run)
    activity = newest_mtime(
        [
            run / "conversation/checkpoint.json",
            run / "conversation/pending-calls.json",
            run / "conversation/research-ledger.jsonl",
            run / "conversation/operational-state.json",
            run / "conversation/trajectory.jsonl",
            run / "model-usage.jsonl",
            run / "supervision.json",
        ]
    )
    state = _state(run, host, supervision, outcome, context_failure, sessions, pending)
    return {
        "id": run.name,
        "started_at": _started_at(run, startup),
        "model": startup.get("model") or startup.get("upstream_model"),
        "host_status": host.get("status"),
        "supervision_phase": supervision.get("phase"),
        "outcome": outcome,
        "context_failure": context_failure,
        "sessions": sessions,
        "pending": pending,
        "decisions": decisions,
        "usage": _usage(run, startup),
        "jobs": _jobs(run),
        "last_activity": activity or _started_at(run, startup),
        "checkpoint_at": mtime(run / "conversation/checkpoint.json"),
        "state": state,
    }


def job_meta(status):
    label, tone = JOB_LABELS.get(status, (status or "未观察", "idle"))
    return {"label": label, "tone": tone}


def summarize_run(run, *, selected_state=None):
    """A lightweight entry for the run switcher and history list."""
    startup = read_json(run / "startup.json", {}) or {}
    host = read_json(run / ".host/state.json", {}) or {}
    supervision = read_json(run / "supervision.json", {}) or {}
    outcome = read_json(run / "conversation/run-outcome.json", {}) or {}
    if selected_state:
        state = selected_state
    elif outcome.get("status") == "blocked":
        state = "blocked"
    elif supervision.get("phase") == "stopped":
        state = "stopped"
    elif host.get("status") == "suspended":
        state = "suspended"
    elif host.get("status") == "running":
        state = "interrupted"
    else:
        state = "stopped"
    label, tone = STATE_LABELS.get(state, STATE_LABELS["unknown"])
    return {
        "id": run.name,
        "started_at": _started_at(Path(run), startup),
        "model": startup.get("model") or startup.get("upstream_model"),
        "state": state,
        "state_label": label,
        "tone": tone,
        "outcome": outcome,
    }
