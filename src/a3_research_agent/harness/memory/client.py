"""Call the run-owned memory service without exposing local execution to the model."""

import os
import time
from urllib.parse import urlsplit

import httpx

_CONTEXT_CACHE = {}


def call(arguments):
    url = os.environ.get("A3_MEMORY_SERVICE_URL", "")
    token = os.environ.get("A3_MEMORY_SERVICE_TOKEN", "")
    address = urlsplit(url)
    if address.scheme != "http" or address.hostname not in {"127.0.0.1", "::1"} or not token:
        return {
            "error": "memory_service_unavailable",
            "hint": "Report missing Host memory service configuration; preserve research progress.",
        }
    try:
        response = httpx.post(
            url.rstrip("/") + "/memory",
            json=arguments,
            headers={"Authorization": "Bearer " + token},
            timeout=90,
            trust_env=False,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        return {
            "error": "memory_service_unavailable",
            "hint": "Memory service request failed; report the failure and preserve research progress.",
        }


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
    key = (os.environ.get("A3_MEMORY_SERVICE_URL"), query, plan.get("revision"))
    cached = _CONTEXT_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < 30:
        return cached[1]
    result = call({"action": "context", "key": query, "limit": 3})
    _CONTEXT_CACHE.clear()
    _CONTEXT_CACHE[key] = (time.monotonic(), result)
    return result


def dispatch(arguments):
    result = call(arguments)
    if arguments.get("action") in {"write", "update", "archive"}:
        _CONTEXT_CACHE.clear()
    return result
