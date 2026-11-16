"""Translate common job operations into the existing owner-checked Host API."""

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

from . import tool_results
from .tool_contracts import (
    ToolInputError,
    arguments,
    boolean,
    definition,
    fail,
    integer,
    relative_name,
    text,
)

STRING = {"type": "string"}
JOB_ID = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
PREPARED = re.compile(r"prepared-[a-f0-9]{32}")
ATTACHMENTS = {
    "type": "array",
    "maxItems": 16,
    "items": {
        "type": "object",
        "properties": {"path": STRING, "destination": STRING},
        "required": ["path"],
        "additionalProperties": False,
    },
}
REQUEST_FIELDS = {
    "command": {"type": "array", "items": STRING, "minItems": 1, "maxItems": 256},
    "gpu": {"type": "boolean"},
    "attachments": ATTACHMENTS,
    "input_exports": {"type": "array", "items": STRING, "maxItems": 64},
    "gpu_memory_mb": {"type": "integer", "minimum": 1},
    "retry_on_gpu_contention": {"type": "boolean"},
    "evaluation": {
        "type": "object",
        "properties": {
            "baseline_job_id": JOB_ID,
            "baseline_manifest": STRING,
            "candidate_manifest": STRING,
        },
        "required": ["baseline_job_id", "baseline_manifest", "candidate_manifest"],
        "additionalProperties": False,
    },
}
ARTIFACT_FIELDS = {
    "job_id": JOB_ID,
    "path": STRING,
    "area": {"type": "string", "enum": ["workspace", "outputs"]},
    "offset": {"type": "integer", "minimum": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 8000},
}
MAX_BATCH = 16
PREPARED_ID = {"type": "string", "pattern": "^prepared-[a-f0-9]{32}$"}
BATCH_FIELDS = {
    "jobs": {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_BATCH,
        "items": {"type": "object", "properties": REQUEST_FIELDS, "required": ["command"]},
    },
    "prepared_ids": {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_BATCH,
        "items": PREPARED_ID,
    },
}
TOOLS = [
    definition(
        "job_submit",
        "Submit work: one prepared GPU job (prepared_id), one CPU command, or a batch. "
        "prepared_ids submits up to 16 saved GPU requests at once; jobs submits up to 16 CPU "
        "requests at once with per-item results. For a GPU job first call job_prepare, wait for "
        "its preflight_job_id, then submit its prepared_id; never repeat the full GPU request. "
        "attachments references workspace text files (scripts and experiment configs); hashes are "
        "automatic. Use input_exports for data and weights. Reconcile submission_unknown by job_id "
        "before retrying.",
        {
            **REQUEST_FIELDS,
            "prepared_id": PREPARED_ID,
            **BATCH_FIELDS,
        },
    ),
    definition(
        "job_prepare",
        "Save GPU requests and run their real CPU preflights. Supply the production command, "
        "attachments and assets once, for one request or up to 16 in jobs. Each entry must handle "
        "A3_RESEARCH_PREFLIGHT=1, load real inputs, perform a minimal forward/data read, write "
        "a3-preflight.json with schema=1,status=passed, and exit. Returns prepared_id and "
        "preflight_job_id per item. Wait for those jobs, then job_submit(prepared_ids=[...]).",
        {**REQUEST_FIELDS, **BATCH_FIELDS},
    ),
    definition(
        "job_status",
        "Read a job's real status and bounded log tail by ID.",
        {"job_id": JOB_ID},
        ["job_id"],
    ),
    definition(
        "job_list",
        "List current or authorized historical jobs; after is a pagination cursor.",
        {
            "scope": STRING,
            "after": STRING,
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
    ),
    definition(
        "job_artifact",
        "Read a bounded UTF-8 page from a job artifact, with source hash. "
        "Use area=outputs for persistent results and workspace for submitted source. "
        "Binary artifacts return type and hash metadata. offset/limit count characters.",
        ARTIFACT_FIELDS,
        ["job_id", "path", "area"],
    ),
    definition(
        "job_cancel",
        "Cancel an owned queued or running job by ID; preserve its files.",
        {"job_id": JOB_ID},
        ["job_id"],
    ),
]
NAMES = frozenset(tool["name"] for tool in TOOLS)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        fail(
            "invalid_job_id",
            "Use the exact 32-character job ID returned by job_submit or job_list.",
        )
    return value


def attachment_references(references, workspace):
    if not isinstance(references, list) or len(references) > 16:
        fail(
            "invalid_attachments",
            "Attach at most 16 workspace text files using path and optional destination.",
        )
    result, total, names = [], 0, set()
    root = workspace.resolve()
    for reference in references:
        arguments(reference, {"path", "destination"})
        original = text(reference.get("path"), "attachment_path")
        if Path(original).is_absolute():
            try:
                original = Path(original).relative_to(root).as_posix()
            except ValueError:
                fail("invalid_attachment_path", "Select a file inside the current workspace.")
        original = relative_name(original, "attachment_path")
        source = root
        for part in PurePosixPath(original).parts:
            source /= part
            if source.is_symlink():
                fail(
                    "invalid_attachment_path",
                    "Attach regular files in workspace; symbolic links are not accepted.",
                )
        if not source.is_relative_to(root) or not source.is_file():
            fail(
                "attachment_missing",
                "Select a regular file inside workspace; use list or search_files first.",
            )
        destination = relative_name(reference.get("destination", original), "destination")
        if destination in names or any(
            PurePosixPath(destination) in PurePosixPath(name).parents
            or PurePosixPath(name) in PurePosixPath(destination).parents
            for name in names
        ):
            fail(
                "conflicting_attachments",
                "Use distinct remote destinations without file/directory conflicts.",
            )
        names.add(destination)
        if source.stat().st_size > 1024 * 1024:
            fail(
                "attachment_too_large",
                "Each text attachment must fit 1 MiB; use exports for large assets.",
            )
        raw = source.read_bytes()
        if len(raw) > 1024 * 1024:
            fail("attachment_too_large", "Each text attachment must fit 1 MiB.")
        try:
            raw.decode("utf-8")
        except UnicodeError:
            fail(
                "binary_attachment",
                "Attach UTF-8 text; reuse data, video and weights through exports.",
            )
        total += len(raw)
        if total > 8 * 1024 * 1024:
            fail("attachments_too_large", "Text attachments must total at most 8 MiB.")
        result.append(
            {
                "path": original,
                "sha256": hashlib.sha256(raw).hexdigest(),
                **({"destination": destination} if destination != original else {}),
            }
        )
    return result


def submit_request(args, workspace, *, default_gpu=False):
    arguments(args, REQUEST_FIELDS)
    command = args.get("command")
    if (
        not isinstance(command, list)
        or not 1 <= len(command) <= 256
        or any(not isinstance(part, str) or "\0" in part for part in command)
        or not command[0].strip()
    ):
        fail(
            "invalid_command", 'Use an argument array, for example ["python", "scripts/train.py"].'
        )
    body = {key: value for key, value in args.items() if key != "attachments"}
    body["gpu"] = boolean(args.get("gpu", default_gpu), "gpu")
    references = attachment_references(args.get("attachments", []), workspace)
    if references:
        body["workspace_files"] = references
    if "gpu_memory_mb" in body:
        integer(body["gpu_memory_mb"], "gpu_memory_mb", 1, 2**31 - 1)
    if "retry_on_gpu_contention" in body:
        boolean(body["retry_on_gpu_contention"], "retry_on_gpu_contention")
    exports = body.get("input_exports", [])
    if (
        not isinstance(exports, list)
        or len(exports) > 64
        or any(
            not isinstance(value, str) or not re.fullmatch(r"export-[a-f0-9]{32}", value)
            for value in exports
        )
    ):
        fail("invalid_input_exports", "Use at most 64 exact export IDs returned by export_create.")
    evaluation = body.get("evaluation")
    if evaluation is not None:
        arguments(evaluation, {"baseline_job_id", "baseline_manifest", "candidate_manifest"})
        identifier(evaluation.get("baseline_job_id"))
        for key in ("baseline_manifest", "candidate_manifest"):
            if not relative_name(evaluation.get(key), key).endswith(".json"):
                fail(
                    "invalid_evaluation_manifest",
                    "Select relative JSON manifests from job outputs.",
                )
    return body


GPU_NEEDS_PREPARATION = (
    "This is a GPU job, so it needs a preparation step first. Send the same request to "
    'job_prepare(jobs=[{...}]) (or job_prepare(command, attachments,...)) — it runs the CPU '
    "preflight and returns prepared_id. Then call job_submit(prepared_id=\"<that id>\")."
)
BATCH_HINT = (
    "Independent commands can go in one call: job_submit(jobs=[{...}, {...}]) submits them "
    "together and returns one result per job."
)


def submission_result(result):
    if isinstance(result, dict) and result.get("ok") and isinstance(result.get("job_id"), str):
        # One call per independent command multiplies latency; say the cheaper route once.
        return {**result, "batch_hint": BATCH_HINT}
    if not isinstance(result, dict) or result.get("http_error") != 502:
        return result
    try:
        detail = json.loads(result.get("body", ""))["detail"]
        job_id = identifier(detail["job_id"])
    except (ValueError, TypeError, KeyError):
        return result
    return {
        **result,
        "status": "submission_unknown",
        "job_id": job_id,
        "status_path": "/jobs/" + job_id,
        "hint": "Read this job_id with job_status or job_list before deciding any next submission.",
    }


def artifact(args, request):
    arguments(args, ARTIFACT_FIELDS)
    job_id = identifier(args.get("job_id"))
    path = relative_name(args.get("path"))
    area = args.get("area", "workspace")
    if area not in {"workspace", "outputs"}:
        fail(
            "invalid_area", "Use workspace for submitted source or outputs for persistent results."
        )
    offset = integer(args.get("offset", 0), "offset", 0, 2**63 - 1)
    limit = integer(args.get("limit", 4000), "limit", 1, 8000)
    result = request(
        "GET",
        f"/jobs/{job_id}/artifacts?"
        + urlencode(
            {
                "path": path,
                "area": area,
                "offset": offset,
                "limit": limit,
            }
        ),
    )
    if not isinstance(result, dict) or result.get("http_error"):
        return result
    source = {"job_id": job_id, "area": area, "path": path}
    encoding = result.get("encoding")
    if encoding == "base64":
        return {
            **source,
            "error": "artifact_pagination_unavailable",
            "hint": "The Host or Controller deployment lacks artifact paging; report the version mismatch.",
        }
    valid = (
        isinstance(result.get("sha256"), str)
        and re.fullmatch(r"[a-f0-9]{64}", result["sha256"])
        and type(result.get("bytes")) is int
        and result["bytes"] >= 0
    )
    if encoding == "utf-8":
        valid = valid and (
            result.get("unit") == "characters"
            and all(type(result.get(key)) is int for key in ("offset", "end", "total"))
            and isinstance(result.get("content"), str)
            and 0 <= result["offset"] <= result["end"] <= result["total"]
            and result["offset"] == min(offset, result["total"])
            and len(result["content"]) == result["end"] - result["offset"] <= limit
            and type(result.get("eof")) is bool
            and result["eof"] == (result["end"] == result["total"])
        )
    elif encoding != "binary":
        valid = False
    if not valid:
        return {
            **source,
            "error": "invalid_artifact_page",
            "hint": "The artifact page is malformed; preserve this response and report the deployment fault.",
        }
    return {**result, **source}


def collection(results):
    """Report every item of a batch, never hiding partial success."""
    items = [tool_results.normalize(value) for value in results]
    failed = [item for item in items if not item["ok"]]
    result = {"items": items, "count": len(items), "failed": len(failed)}
    if failed:
        result["notice"] = "Read every item; some submissions in this batch did not succeed."
        if len(failed) == len(items):
            result["error"] = "batch_failed"
            result["hint"] = (
                "No submission in this batch succeeded; correct each item error before retrying."
            )
    return result


def batch(name, args, request, root):
    """Submit or prepare several independent jobs in one decision turn."""
    if "prepared_ids" in args:
        if name != "job_submit":
            fail("invalid_batch", "job_prepare works from jobs=[...], not prepared_ids.")
        arguments(args, {"prepared_ids"})
        identifiers = args["prepared_ids"]
        valid = (
            isinstance(identifiers, list)
            and 1 <= len(identifiers) <= MAX_BATCH
            and all(isinstance(value, str) and PREPARED.fullmatch(value) for value in identifiers)
        )
        if not valid:
            fail(
                "invalid_prepared_ids",
                f"Submit 1–{MAX_BATCH} exact prepared_id values saved by job_prepare.",
            )
        from . import job_preparation

        return collection([job_preparation.submit(value, request, root) for value in identifiers])
    arguments(args, {"jobs"})
    entries = args["jobs"]
    if (
        not isinstance(entries, list)
        or not 1 <= len(entries) <= MAX_BATCH
        or any(not isinstance(entry, dict) for entry in entries)
    ):
        fail("invalid_jobs", f"Provide 1–{MAX_BATCH} job objects in jobs, each with a command.")
    from . import job_preparation

    results = []
    for entry in entries:
        try:
            if name == "job_prepare":
                job = submit_request(entry, root, default_gpu=True)
                if not job["gpu"]:
                    results.append(
                        {
                            "error": "gpu_preparation_required",
                            "hint": "Submit CPU work directly with job_submit(jobs=[...]).",
                        }
                    )
                    continue
                results.append(job_preparation.prepare(job, request))
            else:
                job = submit_request(entry, root)
                if job["gpu"]:
                    results.append(
                        {
                            "error": "gpu_job_needs_preparation",
                            "hint": GPU_NEEDS_PREPARATION,
                        }
                    )
                    continue
                results.append(submission_result(request("POST", "/jobs", job)))
        except ToolInputError as error:
            results.append({"error": error.code, "hint": error.hint})
    return collection(results)


def dispatch(name, args, request, *, workspace=None):
    try:
        if name in {"job_prepare", "job_submit"}:
            from . import job_preparation

            root = Path(workspace or Path.cwd())
            if "jobs" in args or "prepared_ids" in args:
                return batch(name, args, request, root)
            if name == "job_prepare":
                job = submit_request(args, root, default_gpu=True)
                if not job["gpu"]:
                    fail(
                        "gpu_preparation_required",
                        "Use job_submit(command,attachments) directly for CPU work.",
                    )
                return job_preparation.prepare(job, request)
            if "prepared_id" in args:
                arguments(args, {"prepared_id"})
                return job_preparation.submit(args["prepared_id"], request, root)
            job = submit_request(args, root)
            if job["gpu"]:
                fail("gpu_job_needs_preparation", GPU_NEEDS_PREPARATION)
            return submission_result(request("POST", "/jobs", job))
        if name == "job_artifact":
            return artifact(args, request)
        if name == "job_list":
            arguments(args, {"scope", "after", "limit"})
            parameters = dict(args)
            if "limit" in parameters:
                integer(parameters["limit"], "limit", 1, 200)
            for field in ("scope", "after"):
                if field in parameters:
                    text(parameters[field], field, 200)
            return request("GET", "/jobs" + ("?" + urlencode(parameters) if parameters else ""))
        if name in {"job_status", "job_cancel"}:
            arguments(args, {"job_id"})
            path = "/jobs/" + identifier(args.get("job_id"))
            if name == "job_status":
                return request("GET", path)
            if name == "job_cancel":
                return request("DELETE", path)
        fail("unknown_job_tool", "Use one of the registered job tools.")
    except ToolInputError as error:
        return {"error": error.code, "hint": error.hint}
