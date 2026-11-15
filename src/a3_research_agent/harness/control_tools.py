"""Simple research-status reporting and explicit wait modes."""

from . import operational_state, research_status, waiting
from .tool_contracts import ToolInputError, arguments, definition, fail, integer, text

STRING = {"type": "string"}
TOOLS = [
    definition(
        "job_wait",
        "Wait for selected jobs until one finishes/fails/cancels or an operator/input notification arrives. By default wait without a deadline; set timeout_seconds only for a bounded wait (0 polls once). Omit job_ids to use observed active jobs. No active jobs returns immediately; use idle_wait to pause without jobs.",
        {
            "job_ids": {
                "type": "array",
                "maxItems": 64,
                "items": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
            },
            "timeout_seconds": {"type": "integer", "minimum": 0},
        },
    ),
    definition(
        "idle_wait",
        "Pause for up to seconds (default30), waking for operator/input notifications. Does not poll jobs; use job_wait to await remote work.",
        {"seconds": {"type": "integer", "minimum": 0, "maximum": 600}},
    ),
    definition(
        "research_status",
        "Report whole-run progress or an external blocker needing operator action. Keep tasks, ordinary waiting and research-route blockers in plan. For status=blocked supply kind, required_action and existing workspace evidence paths.",
        {
            "status": {"type": "string", "enum": ["continue", "blocked"]},
            "reason": STRING,
            "kind": {"type": "string", "enum": sorted(research_status.EXTERNAL_BLOCKERS)},
            "required_action": STRING,
            "evidence": {"type": "array", "maxItems": 8, "items": STRING},
        },
        ["status", "reason"],
    ),
]
NAMES = frozenset(tool["name"] for tool in TOOLS)


def dispatch(name, args, request, *, cursor=None):
    try:
        if name == "research_status":
            return research_status.update(args)
        if name == "idle_wait":
            arguments(args, {"seconds"})
            return waiting.wait_idle(
                request, integer(args.get("seconds", 30), "seconds", 0, 600), cursor=cursor
            )
        if name != "job_wait":
            fail("unknown_control_tool", "Use job_wait, idle_wait or research_status")
        arguments(args, {"job_ids", "timeout_seconds"})
        ids = args["job_ids"] if "job_ids" in args else waiting.active_jobs()
        if not isinstance(ids, list) or len(ids) > 64:
            fail(
                "invalid_job_ids",
                "Use at most64 exact job IDs; omit job_ids to wait for active jobs",
            )
        for job in ids:
            value = text(job, "job_id", 32)
            if len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
                fail("invalid_job_ids", "Use exact32-character job IDs returned by job tools")
        timeout = args.get("timeout_seconds")
        if "timeout_seconds" in args and (type(timeout) is not int or timeout < 0):
            fail(
                "invalid_timeout_seconds",
                "Use a nonnegative integer timeout_seconds, or omit it to wait until an event.",
            )
        result = waiting.wait_for_jobs(
            request,
            list(dict.fromkeys(ids)),
            timeout_seconds=timeout,
            cursor=cursor,
        )
        operational_state.record_wait(result)
        return result
    except ToolInputError as error:
        return {"error": error.code, "hint": error.hint}
