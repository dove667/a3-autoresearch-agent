"""Append one durable experiment ledger for daytime analysis.

The trajectory keeps every tool call; this ledger keeps a small ordered record of
job and compaction events that a human can read without expanding archives.
"""

import json
import time

from a3_research_agent.harness import state_paths
from a3_research_agent.harness.context import manager

MAX_BYTES = 512 * 1024
KEEP_LINES = 1200
FIELDS = (
    "job_id",
    "prepared_id",
    "preflight_job_id",
    "export_id",
    "candidate_job_id",
    "baseline_job_id",
    "status",
    "exit_code",
)
RECORDED_TOOLS = frozenset(
    {
        "job_submit",
        "job_prepare",
        "job_status",
        "job_wait",
        "job_cancel",
        "evaluation_compare",
        "export_create",
    }
)


def path():
    return state_paths.path(state_paths.LEDGER)


def append(event):
    """Write one compact event; a ledger problem must never break research."""
    line = json.dumps({"ts": round(time.time(), 3), **event}, ensure_ascii=False)
    try:
        file = path()
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        if file.stat().st_size > MAX_BYTES:
            trim(file)
    except OSError:
        return None
    return line


def trim(file):
    lines = file.read_text(encoding="utf-8").splitlines()[-KEEP_LINES:]
    manager.atomic(file, "\n".join(lines) + "\n")


def from_tool(call, result):
    """Record a job or comparison event with only observed fields."""
    name = call.get("name")
    if name not in RECORDED_TOOLS or not isinstance(result, dict):
        return None
    batch = result.get("jobs")
    if isinstance(batch, list) and batch and all(isinstance(item, dict) for item in batch):
        lines = []
        for item in batch:
            event = {"event": name + "_item", "call_id": call.get("call_id")}
            event.update({field: item.get(field) for field in FIELDS if item.get(field)})
            if len(event) > 2:
                lines.append(append(event))
        return lines or None
    observed = {field: result[field] for field in FIELDS if result.get(field) is not None}
    if name == "job_wait":
        observed["reason"] = result.get("reason")
        observed["jobs"] = sorted(result.get("observed") or result.get("changes") or {})
    if not any(value for key, value in observed.items() if key != "jobs"):
        return None
    event = {"event": name, "call_id": call.get("call_id"), **observed}
    diagnosis = result.get("diagnosis")
    if isinstance(diagnosis, dict):
        event["failure_class"] = diagnosis.get("failure_class")
    return append(event)


def from_compaction(record):
    if not isinstance(record, dict):
        return None
    return append(
        {
            "event": "context_compaction",
            "summary_mode": record.get("summary_mode"),
            "before_bytes": record.get("before_estimated_bytes"),
            "after_bytes": record.get("after_estimated_bytes"),
            "degraded_reason": (record.get("degraded_reason") or "")[:200] or None,
        }
    )
