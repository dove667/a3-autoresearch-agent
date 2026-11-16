"""Verify the current fixed research identity before its next decision."""

import http.client

from a3_research_agent.harness.context import manager as cm

INSTRUCTIONS = (
    "Run the assigned research as one Agent. Submit independent jobs before waiting. "
    "Use the current research protocol and carry findings forward with evidence. "
    "Report Harness and infrastructure faults to the operator."
)


class ResearchStopped(RuntimeError):
    """The operator has explicitly removed this process's research identity."""


def self_status(req):
    try:
        value = req("GET", "/sessions/self", timeout=2)
    except (OSError, TimeoutError, ValueError, http.client.HTTPException) as error:
        raise cm.ContextPending("Host identity service unavailable", retry_after=5) from error
    if not isinstance(value, dict) or value.get("http_error"):
        status = value.get("http_error") if isinstance(value, dict) else None
        raise cm.ContextPending(
            "Cannot verify research identity",
            retry_after=5,
            blocked=status is not None and status not in {408, 429, 500, 502, 503, 504},
        )
    if value.get("is_main") is False:
        raise ResearchStopped("Operator removed this process's main research identity")
    if value.get("is_main") is not True:
        raise cm.ContextPending("Invalid research identity response", retry_after=5, blocked=True)
    return value
