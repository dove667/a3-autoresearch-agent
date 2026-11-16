"""Present every model-visible operation with one success/error contract."""

import json
import re


def failure(code, hint, **details):
    return {"ok": False, "error": code, "hint": hint, **details}


def operation_ids(value):
    patterns = {
        "job_id": r"[0-9a-f]{32}",
        "preflight_job_id": r"[0-9a-f]{32}",
        "prepared_id": r"prepared-[0-9a-f]{32}",
        "export_id": r"export-[0-9a-f]{32}",
    }
    return {
        key: item
        for key, pattern in patterns.items()
        if isinstance(item := value.get(key), str) and re.fullmatch(pattern, item)
    }


CONTROLLER_ERRORS = {
    "FileNotFoundError": (
        "artifact_not_found",
        (
            "Not found in the selected area. workspace is this job's input and source "
            "directory; outputs is the recorded run's shared result directory. Use a relative "
            "path and select the area explicitly; a missing file is not a reason to rerun "
            "the job."
        ),
    ),
    "ValueError": (
        "artifact_request_rejected",
        (
            "The Controller rejected this request: check the path, the area and whether the "
            "job predates recorded outputs; legacy jobs serve workspace files only."
        ),
    ),
}


def rejected(status, body, code, hint):
    """Keep a structured rejection instead of one generic maintainer message."""
    try:
        decoded = json.loads(body or "")
    except (ValueError, TypeError):
        return code, hint
    detail = decoded.get("detail") if isinstance(decoded, dict) else None
    if isinstance(detail, dict):
        for key in ("error", "code"):
            candidate = detail.get(key, "")
            if isinstance(candidate, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", candidate):
                code = candidate
                break
        # Host-supplied hint text may carry private detail; keep our own wording only.
        return code, hint
    if isinstance(detail, str) and detail in CONTROLLER_ERRORS:
        return CONTROLLER_ERRORS[detail]
    return code, hint


def normalize(value):
    if isinstance(value, dict):
        if value.get("status") == "submission_unknown":
            return failure(
                "submission_unknown",
                "Check job_id with job_status before any new submission.",
                status="submission_unknown",
                **operation_ids(value),
            )
        if value.get("http_error"):
            status = value["http_error"]
            code = "host_request_failed"
            hints = {
                401: "Report the authentication failure to the operator.",
                403: "Use an authorized resource ID; report missing access if needed.",
                404: "Check the resource ID and artifact area/path.",
                409: "Read current resource state before retrying.",
                422: "Correct the arguments using this tool's schema.",
                429: "Wait for resource capacity before retrying.",
            }
            hint = hints.get(
                status,
                "Preserve this failure and report the tool and resource IDs to the maintainer.",
            )
            # Unexpected service failures never forward exception strings or diagnostic fields.
            if type(status) is int and 400 <= status < 500:
                code, hint = rejected(status, value.get("body"), code, hint)
            return failure(code, hint, http_status=status, **operation_ids(value))
        if value.get("reason") == "observation_error" and not value.get("error"):
            return {
                **value,
                "ok": False,
                "error": "wait_observation_failed",
                "hint": "The wait could not observe its targets; query their status before continuing.",
            }
        if value.get("error"):
            return {
                **value,
                "ok": False,
                "hint": value.get("hint")
                or "Inspect current state and correct this request before retrying.",
            }
        return {**value, "ok": True}
    if isinstance(value, list):
        return {"ok": True, "items": value}
    return failure(
        "invalid_tool_response",
        "The tool returned an invalid response; report its name to the maintainer.",
    )
