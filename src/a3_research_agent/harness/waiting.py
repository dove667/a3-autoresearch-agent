"""Event-driven job waits and bounded notification-aware idle pauses."""

import json
import math
import os
import pathlib
import re
import time

from a3_research_agent.harness import state_paths
from a3_research_agent.harness.benchmark_report import read_receipt
from a3_research_agent.harness.context.tool_output import select_facts
from a3_research_agent.harness.tool_contracts import ToolInputError

TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})

try:  # Job outputs are read through the job tools' shared lister, never a second parser.
    from a3_research_agent.harness.job_outputs import listing as _outputs_listing
except ImportError:  # pragma: no cover - the lister ships with the job tools
    _outputs_listing = None

MAX_RECEIPT_JOBS = 6
MAX_RECEIPT_OUTPUTS = 12
MAX_RECEIPT_METRICS = 10
MAX_INLINE_TRACE_CHARS = 4000
NONACTIVE_STATES = TERMINAL_STATES | {"observation_error", "submission_unknown", "unobserved"}
RECEIPT_FIELDS = (
    "status",
    "exit_code",
    "error",
    "evaluation",
    "owner_scope",
    "owner_run",
    "access",
    "observed_at",
    "benchmark_receipt",
)


def wait_cursor(messages):
    """Acknowledge notifications only after a completed wait result is visible."""
    calls, cursor = set(), None
    for message in messages:
        if message.get("type") == "function_call" and message.get("name") in {
            "job_wait",
            "idle_wait",
        }:
            calls.add(message.get("call_id"))
        if message.get("type") != "function_call_output" or message.get("call_id") not in calls:
            continue
        raw = message.get("output")
        if not isinstance(raw, str) or len(raw) > 16000:
            continue
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if (
            isinstance(value, dict)
            and value.get("wait_schema") == 1
            and re.fullmatch(r"[0-9a-f]{64}", str(value.get("wake_cursor", "")))
        ):
            cursor = value["wake_cursor"]
    return cursor


def active_jobs():
    path = state_paths.path(state_paths.OPERATIONS)
    if not path.exists():
        return []
    return sorted(
        job
        for job, row in json.loads(path.read_text()).get("jobs", {}).items()
        if row.get("status")
        and row["status"] not in NONACTIVE_STATES
        and re.fullmatch(r"[0-9a-f]{32}", job)
    )


def input_revision(manifest_path=None):
    configured = os.environ.get("A3_INPUT_ROOT")
    path = (
        pathlib.Path(manifest_path)
        if manifest_path
        else pathlib.Path(configured) / "input-manifest.json"
        if configured
        else None
    )
    if path is None or not path.is_file():
        return None
    manifest = json.loads(path.read_text())
    return {key: manifest.get(key) for key in ("status", "revision", "generation", "digest")}


def bounded_seconds(value):
    if type(value) is not int or not 0 <= value <= 600:
        raise ValueError("Wait duration must be an integer in 0..600 seconds")
    return value


def outputs_listing(req, job_id):
    """One job's top-level outputs, or the reason they are unavailable.

    The lister is shared with the job tools so a wait receipt and an artifact read
    name the same files. A missing or faulty lister shortens the receipt; it never
    fails a wait that already observed a terminal state.
    """
    if _outputs_listing is None:
        return {"unavailable": "outputs_lister_missing"}
    try:
        value = _outputs_listing(req, job_id, area="outputs")
    except Exception:  # noqa: BLE001 - a receipt detail must not fail the wait
        return {"unavailable": "outputs_listing_failed"}
    return value if isinstance(value, dict) else {"unavailable": "invalid_listing"}


def entry_view(entry):
    """Keep a name and the size needed to choose the next artifact page."""
    if isinstance(entry, str):
        return {"path": entry[:200]} if entry.strip() else None
    if not isinstance(entry, dict):
        return None
    name = entry.get("path") or entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    view = {"path": name[:200]}
    kind = entry.get("kind") or entry.get("type")
    if isinstance(kind, str) and kind in {"file", "directory", "dir"}:
        view["kind"] = "directory" if kind == "dir" else kind
    size = entry.get("bytes", entry.get("size"))
    if type(size) is int and size >= 0:
        view["bytes"] = size
    return view


def outputs_receipt(listing):
    """Carry a bounded output listing; an unreadable listing degrades to a reason."""
    entries = listing.get("entries") if isinstance(listing, dict) else None
    if not isinstance(entries, list):
        reason = listing.get("unavailable") if isinstance(listing, dict) else None
        reason = reason if isinstance(reason, str) and reason else "outputs_listing_failed"
        return {"outputs_unavailable": reason[:80]}
    items = [view for entry in entries if (view := entry_view(entry))]
    receipt = {"outputs": items[:MAX_RECEIPT_OUTPUTS]}
    if len(items) > MAX_RECEIPT_OUTPUTS:
        receipt["outputs_omitted"] = len(items) - MAX_RECEIPT_OUTPUTS
    return receipt


def metrics_receipt(row):
    """Preview measured values already present in this observation.

    The wait response carries the evaluation diagnostics; reuse the context facts
    selector instead of teaching the receipt its own JSON walk.
    """
    facts, omitted, complete = select_facts(row)
    facts = {key: value for key, value in facts.items() if key not in {"job_id", *RECEIPT_FIELDS}}
    if not facts:
        return {}
    if len(facts) > MAX_RECEIPT_METRICS:
        omitted += len(facts) - MAX_RECEIPT_METRICS
        facts = dict(list(facts.items())[:MAX_RECEIPT_METRICS])
    receipt = {"metrics": facts}
    if omitted:
        receipt["metrics_omitted"] = omitted
    if not complete:
        receipt["metrics_incomplete"] = True
    return receipt


def terminal_receipt(req, row, *, list_outputs=True):
    """One finished job's receipt: outcome, measured values and its outputs."""
    receipt = {key: row[key] for key in RECEIPT_FIELDS if key in row}
    receipt.update(metrics_receipt(row))
    if list_outputs:
        receipt.update(outputs_receipt(outputs_listing(req, row["job_id"])))
    else:
        receipt["outputs_unavailable"] = "batch_limit"
    path = row.get("benchmark_receipt")
    if path and row.get("status") == "completed":
        if list_outputs:
            try:
                result = read_receipt(req, row["job_id"], path)
            except (ToolInputError, OSError, TimeoutError) as error:
                receipt["benchmark_unavailable"] = getattr(error, "code", "transport_error")
            else:
                receipt["benchmark"] = inline_benchmark_receipt(result)
        else:
            receipt["benchmark_unavailable"] = "batch_limit"
    return receipt


def inline_benchmark_receipt(result):
    """Keep every exact score and artifact link within the terminal preview."""
    episodes = []
    remaining = MAX_INLINE_TRACE_CHARS
    for episode in result["episodes"]:
        row = {key: value for key, value in episode.items() if key != "trace_events"}
        if "trace_events" in episode:
            size = len(json.dumps(episode["trace_events"], separators=(",", ":")))
            if size <= remaining:
                row["trace_events"] = episode["trace_events"]
                remaining -= size
            else:
                row["trace_events_omitted"] = True
        episodes.append(row)
    return {**result, "episodes": episodes}


def wait_for_jobs(
    req,
    jobs,
    *,
    timeout_seconds=None,
    cursor=None,
    clock=time.monotonic,
    sleep=time.sleep,
    manifest_path=None,
):
    """Observe selected jobs until an event; a deadline exists only when requested."""
    if timeout_seconds is not None and (type(timeout_seconds) is not int or timeout_seconds < 0):
        raise ValueError("Job wait timeout must be a nonnegative integer or omitted")
    if not jobs:
        return {"wait_schema": 1, "reason": "no_jobs", "elapsed_seconds": 0}
    start = clock()
    deadline = math.inf if timeout_seconds is None else start + timeout_seconds
    initial_inputs = input_revision(manifest_path)

    def result(reason, **fields):
        if reason == "observation_error":
            if cause := fields.pop("error", None):
                fields["cause"] = cause
            fields.update(
                error="wait_observation_failed",
                hint="Observation failed; inspect the referenced job or service before retrying.",
            )
        return {
            "wait_schema": 1,
            "reason": reason,
            "elapsed_seconds": round(clock() - start, 3),
            **({"wake_cursor": cursor} if cursor else {}),
            **fields,
        }

    delay, offset, first, observed = 2, 0, True, {}
    while True:
        current_inputs = input_revision(manifest_path)
        if (
            current_inputs != initial_inputs
            or current_inputs
            and current_inputs.get("status") != "active"
        ):
            return result("inputs_changed")
        remaining = deadline - clock()
        if not first and remaining <= 0:
            return result("timeout", observed=observed)
        first = False
        batch = jobs[offset : offset + 64]
        hold = 25 if timeout_seconds is None else min(25, max(0, math.floor(remaining)))
        body = {"job_ids": batch, "seconds": hold}
        if cursor is not None:
            body["cursor"] = cursor
        started = clock()
        try:
            value = req(
                "POST",
                "/jobs/wait",
                body,
                timeout=max(0.1, min(30, remaining)) if remaining > 0 else 2,
            )
        except (OSError, TimeoutError):
            value = {"http_error": 502}
        if not isinstance(value, dict):
            return result("observation_error", error="invalid_wait_response")
        if value.get("http_error"):
            if value["http_error"] not in {408, 429, 500, 502, 503, 504}:
                return result("observation_error", http_status=value["http_error"])
            if clock() >= deadline:
                return result("timeout", observed=observed)
            sleep(min(delay, max(0, deadline - clock())))
            delay = min(30, delay * 2)
            continue
        delay = 2
        rows = value.get("jobs")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or row.get("job_id") not in batch for row in rows
        ):
            return result("observation_error", error="invalid_wait_response")
        observed.update({"/jobs/" + row["job_id"]: row for row in rows})
        finished = [row for row in rows if row.get("status") in TERMINAL_STATES]
        if finished:
            # One receipt per finished job. The model should not spend a follow-up
            # turn asking for a status or an output listing it already has here.
            terminal = {
                "/jobs/" + row["job_id"]: terminal_receipt(
                    req, row, list_outputs=index < MAX_RECEIPT_JOBS
                )
                for index, row in enumerate(finished)
            }
            return result("state_changed", changes=terminal)
        if value.get("reason") == "notification":
            signals = value.get("signals")
            if not isinstance(signals, dict) or not re.fullmatch(
                r"[0-9a-f]{64}", str(signals.get("cursor", ""))
            ):
                return result("observation_error", error="invalid_notification")
            cursor = signals["cursor"]
            return result(
                "notification",
                notifications={key: signals.get(key) for key in ("feedback", "inputs", "is_main")},
            )
        if value.get("reason") != "timeout":
            observation = value.get("observation")
            return result(
                "observation_error",
                jobs=rows,
                **({"observation": observation} if isinstance(observation, dict) else {}),
            )
        offset = offset + 64 if offset + 64 < len(jobs) else 0
        sleep(min(max(0, 1 - (clock() - started)), max(0, deadline - clock())))


def wait_idle(
    req, seconds=30, *, cursor=None, clock=time.monotonic, sleep=time.sleep, manifest_path=None
):
    """Wait for operator/input notifications without observing remote jobs."""
    bounded_seconds(seconds)
    start = clock()
    deadline, initial_inputs = start + seconds, input_revision(manifest_path)

    def result(reason, **fields):
        if reason == "observation_error":
            if cause := fields.pop("error", None):
                fields["cause"] = cause
            fields.update(
                error="wait_observation_failed",
                hint="Observation failed; inspect the referenced job or service before retrying.",
            )
        return {
            "wait_schema": 1,
            "reason": reason,
            "elapsed_seconds": round(clock() - start, 3),
            **({"wake_cursor": cursor} if cursor else {}),
            **fields,
        }

    while True:
        current_inputs = input_revision(manifest_path)
        if (
            current_inputs != initial_inputs
            or current_inputs
            and current_inputs.get("status") != "active"
        ):
            return result("inputs_changed")
        remaining = deadline - clock()
        if remaining <= 0:
            return result("timeout")
        started = clock()
        try:
            value = req("GET", "/wait/signals", timeout=min(2, remaining))
        except (OSError, TimeoutError):
            return result("observation_error")
        if (
            not isinstance(value, dict)
            or value.get("schema") != 1
            or type(value.get("pending")) is not bool
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("cursor", "")))
        ):
            return result("observation_error", error="invalid_notification")
        changed = value["cursor"] != cursor if cursor is not None else value["pending"]
        cursor = value["cursor"]
        if changed:
            return result(
                "notification",
                notifications={key: value.get(key) for key in ("feedback", "inputs", "is_main")},
            )
        sleep(min(max(0, 2 - (clock() - started)), max(0, deadline - clock())))
