"""Bounded job-target waits and notification-aware idle waits."""

import json
import math
import os
import pathlib
import re
import time

from a3_research_agent.harness import state_paths

TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


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
        if row.get("status") not in TERMINAL_STATES and re.fullmatch(r"[0-9a-f]{32}", job)
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


def wait_for_jobs(
    req,
    jobs,
    *,
    timeout_seconds=600,
    cursor=None,
    clock=time.monotonic,
    sleep=time.sleep,
    manifest_path=None,
):
    """Observe only selected jobs until one terminates, inputs change or timeout."""
    bounded_seconds(timeout_seconds)
    if not jobs:
        return {"wait_schema": 1, "reason": "no_jobs", "elapsed_seconds": 0}
    start = clock()
    deadline, initial_inputs = start + timeout_seconds, input_revision(manifest_path)

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
            "elapsed_seconds": clock() - start,
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
        hold = min(25, max(0, math.floor(remaining)))
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
        terminal = {
            "/jobs/" + row["job_id"]: {
                key: row[key]
                for key in ("status", "exit_code", "error", "evaluation")
                if key in row
            }
            for row in rows
            if row.get("status") in TERMINAL_STATES
        }
        if terminal:
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
            return result("observation_error", jobs=rows)
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
            "elapsed_seconds": clock() - start,
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
