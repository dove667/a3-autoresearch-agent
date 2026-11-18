"""Optional Host-only Responses endpoint for an existing run identity."""

import json
import os

import httpx

from .direct import DirectHTTPError, DirectTransportError, post_json


def context_limit_rejected(error, status):
    """Recognize explicit provider context errors without exposing the payload."""
    if status not in {400, 413, 422}:
        return False
    try:
        body = (
            json.loads(error.body) if isinstance(error, DirectHTTPError) else error.response.json()
        )
    except (AttributeError, ValueError):
        return False
    if not isinstance(body, dict):
        return False
    detail = body.get("error", body)
    if isinstance(detail, dict):
        if detail.get("code") in (
            "context_length_exceeded",
            "context_window_exceeded",
            "input_too_long",
        ):
            return True
        detail = detail.get("message")
    return isinstance(detail, str) and any(
        phrase in detail.casefold()
        for phrase in (
            "maximum context length",
            "exceeds the context window",
            "context length exceeded",
        )
    )


def chatgpt_history(items: list) -> list:
    """Replay portable messages and tool calls from a Responses-compatible upstream."""
    history = []
    for item in items:
        if isinstance(item, dict):
            kind = item.get("type")
            item_id = item.get("id") or ""
            if kind == "reasoning" and not item.get("encrypted_content"):
                continue
            if kind == "message" and item.get("role") == "assistant" and item_id:
                if not item_id.startswith("msg_"):
                    text = "\n".join(
                        part["text"]
                        for part in item["content"]
                        if part.get("type") == "output_text"
                    )
                    item = {"role": "assistant", "content": text}
            elif kind == "function_call" and item_id.startswith("call_"):
                # External providers can reuse a call ID as the response item ID.
                item = {key: item[key] for key in ("type", "call_id", "name", "arguments")}
        history.append(item)
    return history


def configured_upstream(default):
    base_url = os.environ.get("A3_UPSTREAM_RESPONSES_BASE_URL", "").rstrip("/")
    if not base_url:

        def forward_default(*, model, **arguments):
            if model.startswith("chatgpt/") and isinstance(arguments.get("input"), list):
                arguments["input"] = chatgpt_history(arguments["input"])
            return default(model=model, **arguments)

        return forward_default
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

    direct_interface = os.environ.get("A3_UPSTREAM_DIRECT_INTERFACE", "").strip()

    def forward(*, model, **arguments):
        # The saved history can contain reasoning encrypted by a different provider.
        if isinstance(arguments.get("input"), list):
            arguments["input"] = [
                item for item in arguments["input"] if item.get("type") != "reasoning"
            ]
        arguments.pop("include", None)
        arguments.pop("service_tier", None)
        payload = {**arguments, "model": upstream_model}
        if direct_interface:
            # A system TUN proxy can hijack both DNS and the default route. Bind
            # the model call to the physical interface and an explicit public IP.
            try:
                return post_json(
                    base_url + "/responses",
                    {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    payload,
                    read_timeout_seconds,
                )
            except DirectHTTPError:
                raise
            except DirectTransportError as error:
                raise RuntimeError(str(error)) from error
        response = httpx.post(
            base_url + "/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=timeout,
            trust_env=False,
        )
        response.raise_for_status()
        return response.json()

    forward.provider_token_usage = True
    return forward
