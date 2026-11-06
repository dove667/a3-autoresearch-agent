"""Wait for owned jobs, linear trial state and operator notifications."""

import json
import pathlib
import re
import time


def wait_cursor(messages):
    """Only completed wait results acknowledge a notification in existing history."""
    calls = set()
    cursor = None
    for message in messages:
        if message.get("type") == "function_call" and message.get("name") == "wait":
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
            and isinstance(value.get("wake_cursor"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["wake_cursor"])
        ):
            cursor = value["wake_cursor"]
    return cursor


def wait_for_change(
    req,
    seconds=5,
    *,
    clock=time.monotonic,
    sleep=time.sleep,
    manifest_path=pathlib.Path("/run/input/input-manifest.json"),
    signals=None,
    cursor=None,
):
    """Wait without a model round trip; observations never acknowledge Host events."""
    if type(seconds) is not int or not 0 <= seconds <= 600:
        raise ValueError("Wait seconds must be an integer in 0..600")
    start = clock()
    deadline = start + seconds
    current_cursor = cursor
    last_signal = None
    next_status = start
    previous = None

    def result(reason, **fields):
        value = {"wait_schema": 1, "reason": reason, "elapsed_seconds": clock() - start, **fields}
        if current_cursor is not None:
            value["wake_cursor"] = current_cursor
        return value

    def notification():
        nonlocal current_cursor, last_signal
        if signals is None or last_signal is not None and clock() - last_signal < 2:
            return None
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        try:
            value = signals(min(1, remaining))
        except (OSError, TimeoutError):
            return result("observation_error", path="/wait/signals")
        last_signal = clock()
        if (
            not isinstance(value, dict)
            or value.get("schema") != 1
            or not isinstance(value.get("cursor"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["cursor"])
            or type(value.get("pending")) is not bool
        ):
            return result(
                "observation_error",
                path="/wait/signals",
                http_error=value.get("http_error") if isinstance(value, dict) else None,
            )
        changed = (
            value["cursor"] != current_cursor if current_cursor is not None else value["pending"]
        )
        current_cursor = value["cursor"]
        if changed:
            return result(
                "notification",
                notifications={
                    key: value.get(key) for key in ("feedback", "analysis", "inputs", "is_main")
                },
            )
        return None

    paths = []
    operational = pathlib.Path("operational-state.json")
    if operational.exists():
        for job, item in json.loads(operational.read_text()).get("jobs", {}).items():
            if item.get("status") not in {"completed", "cancelled", "failed"} and re.fullmatch(
                r"[A-Za-z0-9_-]{1,100}", job
            ):
                paths.append("/jobs/" + job)

    def inputs():
        if not manifest_path.exists():
            return None
        manifest = json.loads(manifest_path.read_text())
        return {key: manifest.get(key) for key in ("status", "revision", "generation", "digest")}

    if len(paths) > 64:
        return result("observation_error", error="too_many_wait_targets")
    initial_inputs = inputs()
    if initial_inputs is not None and initial_inputs.get("status") != "active":
        return result("inputs_changed")
    while True:
        if inputs() != initial_inputs:
            return result("inputs_changed")
        notice = notification()
        if notice:
            return notice
        if clock() >= deadline:
            return result("deadline", observed=previous or {})
        if clock() >= next_status:
            current = {}
            for path in sorted(set(paths)):
                notice = notification()
                if notice:
                    return notice
                remaining = deadline - clock()
                if remaining <= 0:
                    return result("deadline", observed={**(previous or {}), **current})
                try:
                    value = req("GET", path, timeout=min(2 if signals else 10, remaining))
                except (OSError, TimeoutError):
                    return result("observation_error", path=path)
                if (
                    not isinstance(value, dict)
                    or value.get("http_error")
                    or not isinstance(value.get("status"), str)
                    or not value["status"]
                ):
                    return result(
                        "observation_error",
                        path=path,
                        http_error=value.get("http_error") if isinstance(value, dict) else None,
                    )
                current[path] = {key: value.get(key) for key in ("status", "exit_code", "closed")}
            terminal = any(
                x.get("status") not in {"running", "created", "queued", "pending", "starting"}
                or x.get("closed")
                for x in current.values()
            )
            if terminal or previous is not None and current != previous:
                return result("state_changed", changes=current)
            previous = current
            next_status = clock() + 10
        now = clock()
        if now >= deadline:
            return result("deadline", observed=previous or {})
        sleep(min(2 if signals else 10, deadline - now, max(0, next_status - now)))
