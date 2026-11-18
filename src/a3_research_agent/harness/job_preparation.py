"""Freeze one GPU request, run requested checks, and submit production once."""

import base64
import fcntl
import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from . import job_failure, preflight_runner, state_paths
from .context.manager import atomic

REPORT = "a3-preflight.json"
PREPARED_ID = re.compile(r"prepared-[a-f0-9]{32}")
JOB_ID = re.compile(r"[a-f0-9]{32}")
PATH_LIST = {
    "type": "array",
    "minItems": 1,
    "maxItems": 16,
    "items": {"type": "string", "minLength": 1, "maxLength": 512},
}
PREFLIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "cuda": {
            "type": "boolean",
            "description": "After CPU preflight, run a short GPU preflight on a leased CUDA device.",
        },
        "output_paths": {
            **PATH_LIST,
            "description": "Fresh, non-overlapping paths relative to the shared A3_OUTPUTS.",
        },
        "checkpoint_paths": {
            **PATH_LIST,
            "description": "Expected checkpoint files or directories within a declared output path.",
        },
    },
    "additionalProperties": False,
}


def contract_paths(value, name):
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError(f"Use 1–16 paths in {name}.")
    paths = []
    for item in value:
        if (
            not isinstance(item, str)
            or not 1 <= len(item) <= 512
            or any(character in item for character in "\\\x00")
        ):
            raise ValueError(f"Use canonical paths relative to A3_OUTPUTS in {name}.")
        path = PurePosixPath(item)
        if (
            path.is_absolute()
            or not path.parts
            or path.as_posix() != item
            or any(part.startswith(".") for part in path.parts)
        ):
            raise ValueError(f"Use canonical paths relative to A3_OUTPUTS in {name}.")
        if path in paths:
            raise ValueError(f"Use each path once in {name}.")
        paths.append(path)
    return paths


def normalize_preflight(value):
    if not isinstance(value, dict) or set(value) - PREFLIGHT_SCHEMA["properties"].keys():
        raise ValueError("Use a preflight object with cuda, output_paths, or checkpoint_paths.")
    cuda = value.get("cuda", False)
    if type(cuda) is not bool:
        raise ValueError("Set preflight.cuda to true or false.")
    outputs = (
        contract_paths(value["output_paths"], "output_paths") if "output_paths" in value else []
    )
    checkpoints = (
        contract_paths(value["checkpoint_paths"], "checkpoint_paths")
        if "checkpoint_paths" in value
        else []
    )
    if any(
        left.is_relative_to(right) or right.is_relative_to(left)
        for index, left in enumerate(outputs)
        for right in outputs[index + 1 :]
    ):
        raise ValueError("Declare separate output_paths; a target cannot contain another target.")
    if any(not any(path.is_relative_to(output) for output in outputs) for path in checkpoints):
        raise ValueError("Each checkpoint_paths entry must fall inside a declared output path.")
    result = {}
    if cuda:
        result["cuda"] = True
    if outputs:
        result["output_paths"] = sorted(path.as_posix() for path in outputs)
    if checkpoints:
        result["checkpoint_paths"] = sorted(path.as_posix() for path in checkpoints)
    return result


@contextmanager
def locked(identifier):
    directory = state_paths.path("prepared-jobs")
    directory.mkdir(parents=True, exist_ok=True)
    path = state_paths.path("prepared-jobs/" + identifier + ".json")
    lock = state_paths.path("prepared-jobs/" + identifier + ".lock")
    with lock.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def cpu_request(job):
    request = {
        key: value
        for key, value in job.items()
        if key
        not in {
            "evaluation",
            "benchmark_receipt",
            "gpu_memory_mb",
            "retry_on_gpu_contention",
            "preflight",
        }
    }
    if job.get("preflight", {}).get("output_paths"):
        command = checked_command(job, "cpu")
    else:
        command = ["env", "A3_RESEARCH_PREFLIGHT=1", *job["command"]]
    return {**request, "gpu": False, "command": command}


def checked_command(job, mode):
    source = Path(preflight_runner.__file__).read_text(encoding="utf-8")
    return ["python", "-c", source, mode, json.dumps(job["preflight"]), *job["command"]]


def gpu_request(job):
    request = {
        key: value
        for key, value in job.items()
        if key not in {"evaluation", "benchmark_receipt", "preflight"}
    }
    return {**request, "command": checked_command(job, "cuda")}


def production_request(job):
    request = {key: value for key, value in job.items() if key != "preflight"}
    if job.get("preflight", {}).get("output_paths"):
        request["command"] = checked_command(job, "production")
    return request


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


def prepare(job, request):
    if job.get("preflight") and len(job["command"]) > 251:
        return {
            "error": "invalid_preflight_command",
            "hint": "Keep a checked command within 251 arguments or move parameters into an attachment.",
        }
    digest = hashlib.sha256(json.dumps(job, sort_keys=True).encode()).hexdigest()
    identifier = "prepared-" + digest[:32]
    with locked(identifier) as path:
        record = None
        if path.exists():
            record = json.loads(path.read_text())
            preflight_id = record.get("preflight_job_id")
            if "submission" in record or not preflight_id:
                return preparation_view(record, identifier)
            status = request("GET", "/jobs/" + preflight_id)
            failed = (
                isinstance(status, dict)
                and status.get("job_id") == preflight_id
                and (
                    status.get("status") in {"failed", "cancelled"}
                    or (
                        status.get("status") == "completed"
                        and type(status.get("exit_code")) is int
                        and status["exit_code"] != 0
                    )
                )
            )
            if not failed:
                return preparation_view(record, identifier)
            attempts = [*record.get("previous_preflights", []), preflight_id]
            record = {
                "job": job,
                "fingerprint": digest,
                "previous_preflights": attempts,
                "preflight_submission_unknown": True,
            }
        if record is None:
            record = {"job": job, "fingerprint": digest, "preflight_submission_unknown": True}
        atomic(path, json.dumps(record))
        try:
            result = request("POST", "/jobs", cpu_request(job))
        except Exception:  # noqa: BLE001 - sending may already have created the CPU job
            return preparation_view(record, identifier)
        if preflight_id := submitted_id(result):
            record.update(preflight_job_id=preflight_id, preflight_submission_unknown=False)
        elif rejected_send(result):
            path.unlink()
            return result
        else:
            record["response"] = result
        atomic(path, json.dumps(record))
        return preparation_view(record, identifier)


def preparation_view(record, identifier):
    result = {"prepared_id": identifier}
    if record.get("preflight_job_id"):
        cuda = record["job"].get("preflight", {}).get("cuda")
        return {
            **result,
            "preflight_job_id": record["preflight_job_id"],
            "status": "preflight_submitted",
            "hint": (
                "Wait for preflight_job_id, then call job_submit with prepared_id to start the CUDA probe."
                if cuda
                else "Wait for preflight_job_id, then call job_submit with prepared_id only."
            ),
        }
    return {
        **result,
        "error": "preflight_submission_unknown",
        "hint": "The preflight submission could not be confirmed. Check recent jobs before preparing another request.",
    }


REPORT_LIMIT = 16384
# Keep roughly the same base64/UTF-8 ratio as the old 5500/4096 pair.
REPORT_BASE64_LIMIT = 4 * ((REPORT_LIMIT + 2) // 3)


def report_verdict(response):
    """Return (accepted, problem) so the hint can separate oversize from absent.

    The old 4096-character ceiling rejected a real 6449-character report while
    the JSON was already valid. Do not relax the schema/status check below.
    """
    if not isinstance(response, dict):
        return False, "missing"
    if response.get("encoding") == "base64":
        encoded = response.get("data")
        if not isinstance(encoded, str) or not encoded:
            return False, "missing"
        if len(encoded) > REPORT_BASE64_LIMIT:
            return False, "too_large"
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return False, "malformed"
    elif response.get("encoding") == "utf-8":
        raw = response.get("content")
        if not isinstance(raw, str) or not raw:
            return False, "missing"
        if len(raw) > REPORT_LIMIT:
            return False, "too_large"
        if not response.get("eof", True):
            return False, "truncated"
    else:
        return False, "missing"
    try:
        report = json.loads(raw)
    except (ValueError, UnicodeError, TypeError):
        return False, "malformed"
    if (
        isinstance(report, dict)
        and type(report.get("schema")) is int
        and report["schema"] == 1
        and report.get("status") == "passed"
    ):
        return True, None
    return False, "invalid"


def report_passed(response):
    return report_verdict(response)[0]


REPORT_HINTS = {
    "too_large": (
        f"a3-preflight.json is larger than the {REPORT_LIMIT}-character report limit "
        f"(or its base64 page exceeds {REPORT_BASE64_LIMIT} encoded characters), so it was "
        'not accepted. Write a compact report; schema=1 and status="passed" are still required.'
    ),
    "truncated": (
        "The a3-preflight.json page ended before EOF, so it may be larger than the "
        f"{REPORT_LIMIT}-character report limit. Write a compact report instead of paging a "
        'large one; schema=1 and status="passed" are still required.'
    ),
    "malformed": (
        "The CPU entry returned a3-preflight.json, but it is not valid JSON. Write a JSON "
        'object with schema=1 and status="passed"; do not weaken those fields.'
    ),
    "invalid": (
        "a3-preflight.json was read, but it does not contain integer schema=1 and "
        'status="passed". Keep those required fields; validation has not been relaxed.'
    ),
    "missing": (
        "No a3-preflight.json page was returned. The CPU entry must write it in the job "
        'workspace with schema=1 and status="passed" before exiting.'
    ),
}


def check_preflight(identifier, request, phase):
    status = request("GET", "/jobs/" + identifier)
    key = "gpu_preflight_job_id" if phase == "cuda" else "preflight_job_id"
    if (
        not isinstance(status, dict)
        or status.get("job_id") != identifier
        or status.get("status") != "completed"
        or type(status.get("exit_code")) is not int
        or status["exit_code"] != 0
    ):
        state = status.get("status") if isinstance(status, dict) else None
        if phase == "cuda" and state in {"queued", "created", "running", "restarting", "paused"}:
            return {
                key: identifier,
                "status": "gpu_preflight_pending",
                "hint": "Wait for gpu_preflight_job_id, then call job_submit with the same prepared_id.",
            }
        diagnosis = job_failure.diagnose(status) or {}
        hint = diagnosis.get("suggested_action")
        if hint is None:
            hint = "Check the preflight job status and logs before submitting."
        return {
            "error": "gpu_preflight_not_passed" if phase == "cuda" else "preflight_not_passed",
            key: identifier,
            "preflight_status": state,
            **{
                name: diagnosis[name]
                for name in ("failure_class", "error_line")
                if name in diagnosis
            },
            "hint": hint,
        }
    report = request("GET", f"/jobs/{identifier}/artifacts?area=workspace&path={REPORT}")
    passed, problem = report_verdict(report)
    if not passed:
        hint = REPORT_HINTS[problem]
        if phase == "cuda":
            hint = hint.replace("CPU entry", "CUDA preflight entry")
        return {
            "error": "preflight_report_missing",
            key: identifier,
            "report_problem": problem,
            "hint": hint,
        }
    return None


def rejected_send(result):
    if not isinstance(result, dict):
        return False
    http_error = result.get("http_error")
    return (type(http_error) is int and 400 <= http_error < 500) or (
        result.get("status") == "resource_busy" and not result.get("job_id")
    )


def check_cuda(record, path, identifier, request):
    if probe_id := record.get("gpu_preflight_job_id"):
        return check_preflight(probe_id, request, "cuda")
    unknown = {
        "error": "gpu_preflight_submission_unknown",
        "prepared_id": identifier,
        "hint": "CUDA preflight admission is uncertain. Inspect recent jobs before preparing another request.",
    }
    if record.get("gpu_preflight_submission_unknown"):
        return unknown
    record["gpu_preflight_submission_unknown"] = True
    atomic(path, json.dumps(record))
    try:
        result = request("POST", "/jobs", gpu_request(record["job"]))
    except Exception:  # noqa: BLE001 - the probe may already have been admitted
        return unknown
    probe_id = submitted_id(result)
    if probe_id:
        record["gpu_preflight_job_id"] = probe_id
    if probe_id or rejected_send(result):
        del record["gpu_preflight_submission_unknown"]
        atomic(path, json.dumps(record))
    if probe_id:
        return {
            "prepared_id": identifier,
            "gpu_preflight_job_id": probe_id,
            "status": "gpu_preflight_pending",
            "hint": "Wait for gpu_preflight_job_id, then call job_submit with the same prepared_id.",
        }
    return result if rejected_send(result) else unknown


def submit(identifier, request, workspace):
    from .job_tools import attachment_references, submission_result

    if not isinstance(identifier, str) or not PREPARED_ID.fullmatch(identifier):
        return {
            "error": "invalid_prepared_id",
            "hint": "Use the prepared_id returned by job_prepare.",
        }
    with locked(identifier) as path:
        if not path.is_file():
            return {
                "error": "unknown_prepared_job",
                "hint": "Prepare the GPU request in this run first.",
            }
        record = json.loads(path.read_text())
        if "submission" in record:
            return record["submission"]
        preflight_id = record.get("preflight_job_id")
        if not preflight_id:
            return preparation_view(record, identifier)
        job = record["job"]
        references = job.get("workspace_files", [])
        current = attachment_references(
            [{key: value for key, value in item.items() if key != "sha256"} for item in references],
            workspace,
        )
        if current != references:
            return {
                "error": "prepared_inputs_changed",
                "hint": "An attachment changed after preparation. Prepare the updated request first.",
            }
        failure = check_preflight(preflight_id, request, "cpu")
        if failure is not None:
            return failure
        if job.get("preflight", {}).get("cuda"):
            cuda = check_cuda(record, path, identifier, request)
            if cuda is not None:
                return {"prepared_id": identifier, **cuda}
        unknown = {
            "error": "production_submission_unknown",
            "prepared_id": identifier,
            "hint": "Submission may have executed. Inspect recent jobs; never submit this request again blindly.",
        }
        record["submission"] = unknown
        atomic(path, json.dumps(record))
        try:
            result = submission_result(request("POST", "/jobs", production_request(job)))
        except Exception:  # noqa: BLE001 - unknown sends must remain fenced against replay
            return unknown
        if rejected_send(result):
            del record["submission"]
        else:
            record["submission"] = result
        atomic(path, json.dumps(record))
        return result
