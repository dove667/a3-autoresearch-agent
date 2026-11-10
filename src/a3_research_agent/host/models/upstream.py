"""Optional Host-only Responses endpoint for an existing run identity."""

import os

import httpx


def configured_upstream(default):
    base_url = os.environ.get("A3_UPSTREAM_RESPONSES_BASE_URL", "").rstrip("/")
    if not base_url:
        return default
    api_key = os.environ.get("A3_UPSTREAM_RESPONSES_API_KEY", "").strip()
    upstream_model = os.environ.get("A3_UPSTREAM_RESPONSES_MODEL", "").strip()
    if not api_key or not upstream_model:
        raise ValueError("The Responses upstream requires its API key and model")

    timeout_key = "A3_UPSTREAM_RESPONSES_READ_TIMEOUT_SECONDS"
    read_timeout = os.environ.get(timeout_key, "120")
    try:
        read_timeout_seconds = int(read_timeout)
    except ValueError as error:
        raise ValueError(f"{timeout_key} must be a positive integer") from error
    if read_timeout_seconds <= 0:
        raise ValueError(f"{timeout_key} must be a positive integer")
    timeout = httpx.Timeout(120, connect=10, read=read_timeout_seconds)

    def forward(*, model, **arguments):
        # The saved history can contain reasoning encrypted by a different provider.
        if isinstance(arguments.get("input"), list):
            arguments["input"] = [
                item for item in arguments["input"] if item.get("type") != "reasoning"
            ]
        arguments.pop("include", None)
        arguments.pop("service_tier", None)
        response = httpx.post(
            base_url + "/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json={**arguments, "model": upstream_model},
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    return forward
