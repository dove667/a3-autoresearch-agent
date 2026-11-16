"""Send bounded tool requests to the run-owned local knowledge service."""

import os
from urllib.parse import urlsplit

import httpx


def call(route, arguments, timeout=90):
    if route not in {"/memory", "/skills/search"}:
        raise ValueError("Unsupported knowledge service route")
    url = os.environ.get("A3_KNOWLEDGE_SERVICE_URL", "")
    token = os.environ.get("A3_KNOWLEDGE_SERVICE_TOKEN", "")
    address = urlsplit(url)
    if address.scheme != "http" or address.hostname not in {"127.0.0.1", "::1"} or not token:
        return {
            "error": "knowledge_service_unavailable",
            "hint": "Report missing Host knowledge service configuration; preserve research progress.",
        }
    try:
        response = httpx.post(
            url.rstrip("/") + route,
            json=arguments,
            headers={"Authorization": "Bearer " + token},
            timeout=timeout,
            trust_env=False,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        return {
            "error": "knowledge_service_unavailable",
            "hint": "Local knowledge service request failed; preserve progress and report the failure.",
        }


def call_skill_search(arguments, timeout=90):
    return call("/skills/search", arguments, timeout=timeout)
