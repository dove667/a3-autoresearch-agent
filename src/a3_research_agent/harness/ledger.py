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
    "gpu_preflight_job_id",
    "export_id",
    "candidate_job_id",
    "baseline_job_id",
    "status",
    "exit_code",
)
GPU_FIELDS = ("gpu_util_percent", "gpu_used_mb", "gpu_sampled_at")
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
            event.update({field: item.get(field) for field in GPU_FIELDS if item.get(field)})
            if len(event) > 2:
                lines.append(append(event))
        return lines or None
    observed = {field: result[field] for field in FIELDS if result.get(field) is not None}
    observed.update(
        {field: result.get(field) for field in GPU_FIELDS if result.get(field) is not None}
    )
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


def _measured(record, names, missing):
    """Take the first reported byte count; say why when the result omits it."""
    for name in names:
        value = record.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return missing


def from_compaction(record):
    """Record measured compaction sizes without hiding fields the result omits.

    ``ForkCompactor`` reports a start event with ``snapshot_bytes`` and an applied
    event with ``before_bytes``/``after_bytes``. The compaction result does not
    carry the summary mode or degradation reason, so those fields state why the
    value is unavailable instead of repeating the old all-null ledger rows.
    """
    if not isinstance(record, dict):
        return None
    applied = (
        record.get("kind") == "applied"
        or record.get("status") == "ready"
        or record.get("applied") is True
    )
    pending = "pending:summary_not_applied"
    summary_mode = record.get("summary_mode")
    if not isinstance(summary_mode, str) or not summary_mode:
        summary_mode = "unavailable:summary_mode_not_reported"
    degraded_reason = record.get("degraded_reason")
    if not isinstance(degraded_reason, str) or not degraded_reason:
        degraded_reason = "none" if applied else pending
    event = {
        "event": "context_compaction",
        "kind": record.get("kind") or "unknown",
        "summary_mode": summary_mode[:200],
        "before_bytes": _measured(
            record,
            ("before_bytes", "before_estimated_bytes", "snapshot_bytes"),
            "unavailable:before_bytes_not_reported",
        ),
        "after_bytes": _measured(
            record,
            ("after_bytes", "after_estimated_bytes"),
            "unavailable:after_bytes_not_reported" if applied else pending,
        ),
        "degraded_reason": degraded_reason[:200],
    }
    if isinstance(record.get("strategy"), str):
        event["strategy"] = record["strategy"]
    return append(event)
