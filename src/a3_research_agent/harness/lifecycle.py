"""Verify the current fixed research identity before its next decision."""

INSTRUCTIONS = (
    "Run the assigned research as one Agent. Submit independent jobs before waiting. "
    "Use the current research protocol and carry findings forward with evidence. "
    "Report Harness and infrastructure faults to the operator."
)


def self_status(req):
    value = req("GET", "/sessions/self")
    if not isinstance(value, dict) or value.get("http_error") or value.get("is_main") is not True:
        raise RuntimeError("Cannot verify current research identity; preserve state")
    return value
