"""Keep a context-blocked Agent alive and responsive to operator control."""

import json
import time

from a3_research_agent.harness import lifecycle, state_paths, waiting
from a3_research_agent.harness.context import manager as cm


def wait(error, request, *, log=None, clock=time.monotonic, sleep=time.sleep):
    """Publish a recoverable wait, then return for fresh configuration and sources.

    No research tool or model call is performed here. Native stop/suspend signals
    interrupt the short sleeps; a source update returns to the decision gate.
    """
    status = "blocked" if getattr(error, "blocked", True) else "waiting"
    reason = getattr(error, "reason", str(error))[:500]
    delay = min(30, max(1, float(getattr(error, "retry_after", 30))))
    path = state_paths.path(state_paths.CONTEXT_FAILURE)
    previous = {}
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    changed = not isinstance(previous, dict) or (
        previous.get("status"),
        previous.get("reason"),
    ) != (status, reason)
    now = time.time()
    state = {
        "status": status,
        "reason": reason,
        "updated_at": now,
        "since": now if changed else previous.get("since", now),
        "recheck_seconds": delay,
        "action": "Waiting in this process; original context and journal remain authoritative.",
    }
    try:
        cm.atomic(path, json.dumps(state))
    except OSError:
        state["status_persistence"] = "unavailable"
    if changed:
        emit(log, {"context_recovery": state})
    deadline, next_control = clock() + delay, clock()
    initial_inputs = input_revision()
    while clock() < deadline:
        if input_revision() != initial_inputs:
            return
        if clock() >= next_control:
            try:
                lifecycle.self_status(request)
            except cm.ContextPending:
                pass
            next_control = clock() + 5
        sleep(min(1, max(0, deadline - clock())))


def input_revision():
    try:
        return waiting.input_revision()
    except (OSError, ValueError):
        return None


def clear(*, log=None):
    """Remove a stale waiting indicator once a normal decision can proceed."""
    path = state_paths.path(state_paths.CONTEXT_FAILURE)
    if path.exists():
        try:
            path.unlink()
        except OSError as error:
            raise cm.ContextPending("Cannot clear context wait state", blocked=True) from error
        emit(log, {"context_recovery": {"status": "recovered"}})


def emit(log, event):
    if log:
        try:
            log(event)
        except OSError:
            pass
