"""Task dispatch and status interface exposed to the main Agent."""

import re

from .tool_contracts import boolean, definition, fail, integer, text

TASK_ID = re.compile(r"[0-9a-f]{32}")
TOOLS = [
    definition(
        "deep_research",
        "Delegate a public-source research question to the read-only Deep Research Agent. "
        "Use submit with the research question, what decision it informs, and a brief context. "
        "Use status with task_id to collect its report and verified source links. The task runs "
        "in the background. Set wait_seconds when other work is unavailable. Reuse the report in "
        "plan routes[].research. Retry only an interrupted or failed task using the original inputs.",
        {
            "action": {"type": "string", "enum": ["submit", "status"]},
            "question": {"type": "string", "maxLength": 1200},
            "context": {"type": "string", "maxLength": 4000},
            "task_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 25},
            "retry": {"type": "boolean"},
        },
        ["action"],
    ),
]


def dispatch(args, request):
    action = args.get("action")
    if action == "submit" and not args.keys() - {"action", "question", "context", "retry"}:
        body = {"question": text(args.get("question"), "question", 1200)}
        if "context" in args:
            body["context"] = text(args["context"], "context", 4000)
        if "retry" in args:
            body["retry"] = boolean(args["retry"], "retry")
        return request("POST", "/deep-research", body)
    if action == "status" and not args.keys() - {"action", "task_id", "wait_seconds"}:
        key = args.get("task_id")
        if not isinstance(key, str) or not TASK_ID.fullmatch(key):
            fail("invalid_task_id", "Use the task_id returned by deep_research submit.")
        wait = integer(args.get("wait_seconds", 0), "wait_seconds", 0, 25)
        return request("GET", f"/deep-research/{key}?wait_seconds={wait}")
    fail("invalid_research_action", "Use submit with a question or status with a task_id.")
