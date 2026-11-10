"""Pair an actual CPU load check with a matching production job request."""

import base64
import hashlib
import json
import re
from pathlib import Path

from a3_research_agent.harness.context.manager import atomic

RECEIPTS = Path(".research-preflight")
REPORT = "a3-preflight.json"
JOB_ID = re.compile(r"[0-9a-f]{32}")


def cpu_request(job):
    """Keep the entry point and inputs identical, and request its CPU check mode."""
    request = {
        key: value
        for key, value in job.items()
        if key not in {"evaluation", "gpu_memory_mb", "retry_on_gpu_contention"}
    }
    return {
        **request,
        "gpu": False,
        "command": ["env", "A3_RESEARCH_PREFLIGHT=1", *job["command"]],
    }


def submitted_id(result):
    if not isinstance(result, dict):
        return None
    identifier = result.get("job_id")
    if identifier is None and result.get("http_error") == 502:
        try:
            identifier = json.loads(result.get("body", ""))["detail"]["job_id"]
        except (ValueError, TypeError, KeyError):
            return None
    return identifier if isinstance(identifier, str) and JOB_ID.fullmatch(identifier) else None


def report_passed(response):
    if not isinstance(response, dict) or response.get("encoding") != "base64":
        return False
    encoded = response.get("data")
    if not isinstance(encoded, str) or len(encoded) > 5500:
        return False
    try:
        report = json.loads(base64.b64decode(encoded, validate=True))
    except (ValueError, UnicodeError):
        return False
    return (
        isinstance(report, dict)
        and type(report.get("schema")) is int
        and report["schema"] == 1
        and report.get("status") == "passed"
    )


def dispatch(arguments, request):
    action = arguments.get("action")
    job = arguments.get("job")
    command = job.get("command") if isinstance(job, dict) else None
    if (
        action not in ("check", "submit")
        or not isinstance(job, dict)
        or job.get("gpu") is not True
        or not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) for part in command)
    ):
        return {
            "error": "invalid_preflight_request",
            "hint": "Pass check or submit and the production GPU job",
        }
    fingerprint = hashlib.sha256(json.dumps(job, sort_keys=True).encode()).hexdigest()
    if action == "check":
        result = request("POST", "/jobs", cpu_request(job))
        if identifier := submitted_id(result):
            atomic(RECEIPTS / f"{identifier}.json", json.dumps({"fingerprint": fingerprint}))
        return result

    identifier = arguments.get("preflight_id")
    if not isinstance(identifier, str) or not JOB_ID.fullmatch(identifier):
        return {"error": "preflight_id_required"}
    path = RECEIPTS / f"{identifier}.json"
    if not path.is_file():
        return {"error": "unknown_preflight"}
    receipt = json.loads(path.read_text())
    if receipt["fingerprint"] != fingerprint:
        return {"error": "preflight_inputs_changed", "hint": "Check the revised job first"}
    if "submission" in receipt:
        return receipt["submission"]

    status_path = f"/jobs/{identifier}"
    status = request("GET", status_path)
    if (
        not isinstance(status, dict)
        or status.get("job_id") != identifier
        or status.get("status") != "completed"
        or type(status.get("exit_code")) is not int
        or status["exit_code"] != 0
    ):
        return {"error": "preflight_not_passed", "status_path": status_path}
    report_path = f"{status_path}/artifacts?area=workspace&path={REPORT}"
    if not report_passed(request("GET", report_path)):
        return {"error": "preflight_report_missing", "report_path": report_path}

    # A crash while sending the job must not silently repeat a production run.
    receipt["submission"] = {"error": "production_submission_unknown", "preflight_id": identifier}
    atomic(path, json.dumps(receipt))
    result = request("POST", "/jobs", job)
    if isinstance(result, dict) and 400 <= result.get("http_error", 0) < 500:
        del receipt["submission"]
    else:
        receipt["submission"] = result
    atomic(path, json.dumps(receipt))
    return result
