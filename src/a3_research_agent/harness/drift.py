"""Keep a drifting model inside real tool use with bounded corrections.

A weak model can answer with prose, repeat one rejected call, or loop on the same
arguments. The guard turns that observation into one short correction and a bounded
forced tool choice; it never decides research content and never blocks the run.
"""

INVALID_ARGUMENT_ERRORS = frozenset(
    {
        "invalid_tool_json",
        "invalid_arguments",
        "missing_arguments",
        "unknown_tool",
        "invalid_batch",
        "invalid_prepared_ids",
        "invalid_jobs",
    }
)
POLLING_TOOLS = frozenset({"job_wait", "idle_wait", "job_status", "job_list", "gpu_status"})
NO_TOOL_NOTICE = (
    "No tool call was received. Continue the research with high-level tool calls: read the "
    "existing evidence, submit or check a job, or record the plan decision."
)
REPEAT_NOTICE = (
    "The same tool call repeated without new information. Read the stored result, check the "
    "observed job state, or record the plan decision instead of repeating it."
)
INVALID_NOTICE = (
    "A tool call was rejected by its own contract. Read the error and hint, then resend the "
    "call with the required fields, or choose the tool that matches the actual need."
)
FORCE_ATTEMPTS = 2
MAX_INCIDENTS = 3


class DriftGuard:
    """Count drift inside one process and bound how often forcing is repeated."""

    def __init__(self, force_attempts=FORCE_ATTEMPTS, max_incidents=MAX_INCIDENTS):
        self.force_attempts = force_attempts
        self.max_incidents = max_incidents
        self.force = 0
        self.no_tool = 0
        self.invalid = 0
        self.repeat = 0
        self.incidents = 0
        self.last = None

    def force_tool_choice(self):
        return self.force > 0

    def spent(self):
        """One model request consumed part of a forced turn."""
        if self.force > 0:
            self.force -= 1

    def observe(self, calls, invalid):
        """Return a correction notice when this tool response drifted, else None."""
        self.invalid = self.invalid + 1 if invalid else 0
        signature = sorted({(call.get("name"), call.get("arguments")) for call in calls})
        repeated = bool(signature) and signature == self.last
        self.last = signature or self.last
        self.repeat = self.repeat + 1 if repeated else 0
        polled = any(name in POLLING_TOOLS for name, _ in signature)
        drifted = invalid or (self.repeat >= 1 and not polled)
        self.incidents = self.incidents + 1 if drifted else 0
        if not drifted or self.incidents > self.max_incidents:
            return None
        self.force = self.force_attempts
        if invalid:
            return INVALID_NOTICE
        return REPEAT_NOTICE if repeated else NO_TOOL_NOTICE

    def observe_empty(self):
        """A response without any tool call is always corrected."""
        self.no_tool += 1
        self.force = self.force_attempts
        return NO_TOOL_NOTICE

    def snapshot(self):
        return {
            "force": self.force,
            "no_tool": self.no_tool,
            "invalid": self.invalid,
            "repeat": self.repeat,
            "incidents": self.incidents,
        }
