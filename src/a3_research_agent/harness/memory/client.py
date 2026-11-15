"""Call the run-owned memory service without exposing local execution to the model."""

import os
import time

from a3_research_agent.harness import knowledge_client

_CONTEXT_CACHE = {}


def call(arguments):
    return knowledge_client.call("/memory", arguments)


def suggestions(plan):
    """Refresh a small experience view when the research focus changes or notes age."""
    direction = plan.get("direction", {})
    focused = [plan.get("steps", {}).get(key, {}) for key in plan.get("focus", [])]
    query = " ".join(
        [
            direction.get("objective", ""),
            *[step.get("task", "") for step in focused],
            *[route.get("hypothesis", "") for route in plan.get("routes", {}).values()],
        ]
    )[:1200]
    key = (os.environ.get("A3_KNOWLEDGE_SERVICE_URL"), query, plan.get("revision"))
    cached = _CONTEXT_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < 30:
        return cached[1]
    result = call({"action": "context", "query": query, "limit": 3})
    _CONTEXT_CACHE.clear()
    _CONTEXT_CACHE[key] = (time.monotonic(), result)
    return result


def dispatch(arguments):
    result = call(arguments)
    if arguments.get("action") in {"write", "update", "archive", "restore"}:
        _CONTEXT_CACHE.clear()
    return result
