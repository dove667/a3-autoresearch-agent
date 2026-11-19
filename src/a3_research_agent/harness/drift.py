"""Keep a drifting model inside real tool use with bounded corrections.

A weak model can answer with prose, repeat one rejected call, loop on the same
arguments, or read for hour without deciding. The guard turns that observation into one short correction and a bounded
forced tool choice; it never decides research content and never blocks the run.
"""

import json

INVALID_ARGUMENT_ERRORS = frozenset(
    {
        "invalid_tool_json",
        "invalid_arguments",
        "missing_arguments",
        "unknown_tool",
        "invalid_batch",
        "invalid_prepared_ids",
        "invalid_jobs",
        "gpu_job_needs_preparation",
        "gpu_preparation_required",
        "prepared_id_unknown",
    }
)
POLLING_TOOLS = frozenset({"job_wait", "idle_wait", "job_status", "job_list", "gpu_status"})
READ_ONLY_TOOLS = frozenset(
    {
        "read",
        "list",
        "search_files",
        "recall",
        "job_status",
        "job_list",
        "job_wait",
        "job_artifact",
        "remote_assets",
        "gpu_status",
        "export_list",
        "idle_wait",
    }
)
READ_ONLY_ACTIONS = {
    "plan": {"show"},
    "memory": {"read", "list", "search", "sources"},
    "skills": {"list", "read", "read_resource"},
}
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
    "call with the required fields, or choose the tool that matches the actual need. If the "
    "arguments were cut off by the response limit, send a shorter first version and extend it "
    "with follow-up edits instead of resending the whole content."
)
# A nudge, never a gate: the model keeps its tools and the run never ends because of it.
ANSWER_TURNS = 5
ANSWER_NOTICE = (
    "Several turns of reading without a recorded decision. In your next message state the facts "
    "you have, what they imply, and the single next action, then continue the work."
)
FORCE_ATTEMPTS = 2
# A weak model can produce a run of rejected calls before it recovers, so each failure mode
# gets its own consecutive budget. Ending the process is the last resort for unattended
# research; one shared three-strike counter made a single bad streak end the whole night.
MAX_INVALID = 8
MAX_REPEAT = 4
MAX_NO_TOOL = 12


def read_only(call):
    """A call that inspects existing state without changing research state."""
    name = call.get("name")
    if name in READ_ONLY_TOOLS:
        return True
    actions = READ_ONLY_ACTIONS.get(name)
    if actions is None:
        return False
    try:
        arguments = json.loads(call.get("arguments") or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(arguments, dict) and arguments.get("action", "show") in actions


class DriftGuard:
    """Count drift inside one process and bound how often forcing is repeated."""

    def __init__(
        self,
        force_attempts=FORCE_ATTEMPTS,
        max_invalid=MAX_INVALID,
        max_repeat=MAX_REPEAT,
        max_no_tool=MAX_NO_TOOL,
        remind=True,
    ):
        self.force_attempts = force_attempts
        self.max_invalid = max_invalid
        self.max_repeat = max_repeat
        self.max_no_tool = max_no_tool
        self.remind = remind
        self.force = 0
        self.no_tool = 0
        self.invalid = 0
        self.repeat = 0
        self.incidents = 0
        self.reading = 0
        self.answers = 0
        self.last = None

    def force_tool_choice(self):
        return self.force > 0

    def spent(self):
        """One model request consumed part of a forced turn."""
        if self.force > 0:
            self.force -= 1

    def observe(self, calls, invalid):
        """Return a correction notice for this tool response, else None."""
        self.exploration(calls)
        self.no_tool = 0
        self.invalid = self.invalid + 1 if invalid else 0
        signature = sorted({(call.get("name"), call.get("arguments")) for call in calls})
        repeated = bool(signature) and signature == self.last
        self.last = signature or self.last
        self.repeat = self.repeat + 1 if repeated else 0
        polled = any(name in POLLING_TOOLS for name, _ in signature)
        drifted = invalid or (self.repeat >= 1 and not polled)
        self.incidents = self.incidents + 1 if drifted else 0
        notice = None
        if invalid and self.invalid <= self.max_invalid:
            self.force = self.force_attempts
            notice = INVALID_NOTICE
        elif self.repeat >= 1 and not polled and self.repeat <= self.max_repeat:
            self.force = self.force_attempts
            notice = REPEAT_NOTICE if repeated else NO_TOOL_NOTICE
        return self.demand_answer() or notice

    def demand_answer(self):
        """Long stretches of reading end with a message instead of more reading."""
        if not self.remind or self.reading < ANSWER_TURNS:
            return None
        self.answers += 1
        self.reading = 0
        return ANSWER_NOTICE

    def exploration(self, calls):
        """Count consecutive turns that only inspect existing state."""
        if not calls or not all(read_only(call) for call in calls):
            self.reading = 0
            return
        self.reading += 1

    def observe_empty(self):
        """A response without any tool call is always corrected."""
        self.no_tool += 1
        self.force = self.force_attempts
        return NO_TOOL_NOTICE

    def stalled(self):
        """The model has spent one consecutive correction budget without acting.

        Stopping is the last resort, reserved for a model that keeps failing the same
        way; isolated bad turns and long recovery attempts leave the run alive.
        """
        return (
            self.no_tool >= self.max_no_tool
            or self.repeat > self.max_repeat
            or self.invalid > self.max_invalid
        )

    def snapshot(self):
        return {
            "force": self.force,
            "no_tool": self.no_tool,
            "invalid": self.invalid,
            "repeat": self.repeat,
            "incidents": self.incidents,
            "reading": self.reading,
            "answers": self.answers,
        }
