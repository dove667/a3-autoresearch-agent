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
    "call with the required fields, or choose the tool that matches the actual need."
)
ANSWER_TURNS = 5
MAX_ANSWERS = 3
ANSWER_NOTICE = (
    "Reading is not producing a decision. Answer now in one plain-text message: the facts you "
    "have, what they imply, and the single next action. Tools are withheld for this turn."
)
FORCE_ATTEMPTS = 2
MAX_INCIDENTS = 3
MAX_NO_TOOL = 6


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
        max_incidents=MAX_INCIDENTS,
        max_no_tool=MAX_NO_TOOL,
        max_answers=MAX_ANSWERS,
    ):
        self.force_attempts = force_attempts
        self.max_answers = max_answers
        self.max_incidents = max_incidents
        self.max_no_tool = max_no_tool
        self.force = 0
        self.no_tool = 0
        self.invalid = 0
        self.repeat = 0
        self.incidents = 0
        self.reading = 0
        self.answers = 0
        self.demanding = False
        self.last = None

    def force_tool_choice(self):
        return self.force > 0

    def spent(self):
        """One model request consumed part of a forced turn."""
        if self.force > 0:
            self.force -= 1

    def wants_answer(self):
        """Tools are withheld this turn so the model has to state a conclusion."""
        return self.demanding

    def answered(self):
        """The demanded message arrived; resume ordinary tool use."""
        self.demanding = False
        self.reading = 0

    def observe(self, calls, invalid):
        """Return a correction notice for this tool response, else None."""
        self.exploration(calls)
        self.invalid = self.invalid + 1 if invalid else 0
        signature = sorted({(call.get("name"), call.get("arguments")) for call in calls})
        repeated = bool(signature) and signature == self.last
        self.last = signature or self.last
        self.repeat = self.repeat + 1 if repeated else 0
        polled = any(name in POLLING_TOOLS for name, _ in signature)
        drifted = invalid or (self.repeat >= 1 and not polled)
        self.incidents = self.incidents + 1 if drifted else 0
        if drifted and self.incidents <= self.max_incidents:
            self.force = self.force_attempts
            if invalid:
                return INVALID_NOTICE
            return REPEAT_NOTICE if repeated else NO_TOOL_NOTICE
        return self.demand_answer()

    def demand_answer(self):
        """Long stretches of reading end with a message instead of more reading."""
        if self.answers >= self.max_answers or self.reading < ANSWER_TURNS:
            return None
        if self.reading % ANSWER_TURNS:
            return None
        self.answers += 1
        self.demanding = True
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
        """Every correction was already spent and the model still does not act.

        Continuing would spend the whole night on rejected requests, so the process
        ends and leaves the drift record for the operator instead.
        """
        return (
            self.no_tool >= self.max_no_tool
            or self.incidents > self.max_incidents
            # Answers were demanded and refused; more reading will not decide either.
            or (self.answers >= self.max_answers and self.reading >= ANSWER_TURNS * 2)
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
