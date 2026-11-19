"""Extract provider validation details without copying provider text."""

import json
import re

ERROR_LABELS = {
    "bad_request",
    "context_length_exceeded",
    "invalid_api_key",
    "invalid_parameter",
    "invalid_request",
    "invalid_request_error",
    "invalid_type",
    "invalid_value",
    "missing_required_parameter",
    "model_not_found",
    "unknown_parameter",
    "unsupported_parameter",
    "unsupported_value",
    "validation_error",
    "invalidparameter",
    "invalidparameter.unknown",
    "missingparameter",
}
REQUEST_ID_HEADERS = {
    "request_id": (
        "llm_provider-x-request-id",
        "x-request-id",
        "x-tt-logid",
        "x-ms-request-id",
        "request-id",
    ),
    "trace_id": ("llm_provider-x-trace-id", "x-trace-id"),
    "proxy_call_id": ("x-litellm-call-id",),
    "backend_id": ("x-litellm-model-id",),
}
REQUEST_ID = re.compile(r"(?:req[-_])?[0-9a-fA-F-]{16,64}")
PARAMETER = re.compile(
    r"(?:input|tools|tool_choice|instructions|max_output_tokens|model|reasoning|stream|"
    r"temperature|top_p|previous_response_id|response_format|store|text)"
    r"(?:\[\d{1,6}\]|\.(?:type|role|content|arguments|call_id|name|parameters|"
    r"output|image_url|effort|format|strict|description|\d{1,6}))*"
)
MISSING_PARAMETER = re.compile(
    r"\bmissing (?:required )?(?:field|parameter|argument)\s*:?\s*"
    r"['\"`]([A-Za-z0-9_.\[\]]{1,160})['\"`]",
    re.IGNORECASE,
)
REASONS = {
    "context_limit": r"context (?:length|window|limit)|too many input tokens",
    "output_token_parameter": r"max_output_tokens|maximum output (?:length|tokens)",
    "tool_history_mismatch": r"no tool (?:call|output)|tool call.{0,40}(?:match|missing|without)",
    "invalid_tool_choice": r"tool_choice|tool choice",
    "unsupported_parameter": r"(?:unknown|unrecognized|unsupported) (?:field|parameter)",
    "missing_parameter": (
        r"(?:required|missing) (?:field|parameter|argument)"
        r"|(?:field|parameter|argument).{0,30} is required"
    ),
    "invalid_json": r"(?:invalid|malformed) json|json (?:parse|decode)",
    "invalid_image": r"input_image|image_url|invalid image|unsupported image",
}


def validation_details(error: Exception) -> dict:
    response = getattr(error, "response", None)
    headers = getattr(error, "response_headers", None) or getattr(response, "headers", {})
    details = {}
    for field, names in REQUEST_ID_HEADERS.items():
        for name in names:
            value = headers.get(name, "")
            if isinstance(value, str) and REQUEST_ID.fullmatch(value):
                details[field] = value
                break

    body = getattr(error, "body", None)
    if body is None and response is not None:
        try:
            body = response.text
        except (AttributeError, ValueError, RuntimeError):
            body = None
    if isinstance(body, str):
        try:
            body = json.loads(body[:16384])
        except ValueError:
            body = None
    if not isinstance(body, dict):
        return details

    problem = body.get("error", body)
    metadata = body.get("ResponseMetadata")
    if isinstance(metadata, dict):
        problem = metadata.get("Error", problem)
    if not isinstance(problem, dict):
        return details
    for field in ("code", "type"):
        value = problem.get(field, problem.get(field.capitalize()))
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            value = str(value)
            if value.lower() in ERROR_LABELS or re.fullmatch(r"[1-9][0-9]{2,6}", value):
                details[field] = value.lower()
    for field in ("param", "parameter", "Param"):
        value = problem.get(field)
        if isinstance(value, str) and len(value) <= 160 and PARAMETER.fullmatch(value):
            details["param"] = value
            break
    message = problem.get("message", problem.get("Message"))
    if isinstance(message, str):
        reasons = [
            reason
            for reason, pattern in REASONS.items()
            if re.search(pattern, message, re.IGNORECASE)
        ]
        if reasons:
            details["reasons"] = reasons
        if "param" not in details and (missing := MISSING_PARAMETER.search(message)):
            parameter = missing.group(1)
            if PARAMETER.fullmatch(parameter):
                details["param"] = parameter
    return details
