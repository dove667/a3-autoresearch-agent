"""Translate common job operations into the existing owner-checked Host API."""

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

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
SOURCE_FILES = {
    "type": "array",
    "maxItems": 16,
    "items": {
        "type": "object",
        "properties": {"path": STRING, "destination": STRING},
        "required": ["path"],
        "additionalProperties": False,
    },
}
SUBMIT_FIELDS = {
    "command": {"type": "array", "items": STRING, "minItems": 1, "maxItems": 256},
    "gpu": {"type": "boolean"},
    "workspace_files": SOURCE_FILES,
    "files": {"type": "object", "additionalProperties": STRING},
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
PREFLIGHT_JOB = {
    "type": "object",
    "properties": {
        **SUBMIT_FIELDS,
        "gpu": {"type": "boolean", "const": True},
        "workspace_files": {
            **SOURCE_FILES,
            "items": {
                **SOURCE_FILES["items"],
                "properties": {
                    **SOURCE_FILES["items"]["properties"],
                    "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                },
            },
        },
    },
    "required": ["command", "gpu"],
    "additionalProperties": False,
}
ARTIFACT_FIELDS = {
    "job_id": JOB_ID,
    "path": STRING,
    "area": {"type": "string", "enum": ["workspace", "outputs"]},
    "offset": {"type": "integer", "minimum": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 8000},
}
TOOLS = [
    definition(
        "job_submit",
        "Submit one remote command as an argument array. Attach local scripts with workspace_files "
        "using only path and optional destination; the tool computes SHA256. Attachments are "
        "scripts/ UTF-8 files, at most 1 MiB each and "
        "8 MiB together. Use input_exports for binary assets, "
        "existing data or weights. Defaults gpu=false. Submit independent jobs, then wait. "
        "A submission_unknown response must be reconciled by its job_id, never resubmitted blindly.",
        SUBMIT_FIELDS,
        ["command"],
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
    definition(
        "job_export",
        "Seal selected job files as an immutable read-only export for reuse. "
        "Creation is asynchronous; use the returned export ID through the Host export APIs.",
        {
            "job_id": JOB_ID,
            "path": STRING,
            "area": {"type": "string", "enum": ["workspace", "outputs"]},
        },
        ["job_id", "path", "area"],
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


def workspace_references(references, workspace, *, allow_pinned_sources=False):
    if not isinstance(references, list) or len(references) > 16:
        fail(
            "invalid_workspace_files",
            "Attach at most 16 workspace files using path and optional destination.",
        )
    result, total, names = [], 0, set()
    root = workspace.resolve()
    for reference in references:
        allowed = {"path", "destination"}
        if allow_pinned_sources:
            allowed.add("sha256")
        arguments(reference, allowed)
        original = text(reference.get("path"), "source_path")
        source = (root / original).resolve()
        if not source.is_relative_to(root) or source == root:
            fail("invalid_source_path", "Attach a regular file inside the current workspace.")
        relative = source.relative_to(root).as_posix()
        relative_name(relative, "source_path")
        parts = PurePosixPath(relative).parts
        if len(parts) < 2 or parts[0] != "scripts":
            fail(
                "invalid_source_path",
                "Attach source under scripts/; use destination to rename it in the remote workspace.",
            )
        destination = relative_name(reference.get("destination", relative), "destination")
        if destination in names:
            fail("duplicate_destination", "Each attachment must have a different destination.")
        names.add(destination)
        if not source.is_file():
            fail(
                "source_file_missing",
                "The attachment is missing or is a directory; use search_files or list.",
            )
        if source.stat().st_size > 1024 * 1024:
            fail(
                "source_file_too_large",
                "Each source attachment must be at most 1 MiB; split code or use an export for large assets.",
            )
        raw = source.read_bytes()
        try:
            raw.decode("utf-8")
        except UnicodeError:
            fail(
                "binary_source_file",
                "Attach UTF-8 source files; use input_exports for binary assets.",
            )
        total += len(raw)
        if len(raw) > 1024 * 1024:
            fail("source_file_too_large", "Each source attachment must be at most 1 MiB.")
        if total > 8 * 1024 * 1024:
            fail(
                "source_attachments_too_large",
                "Source attachments must total at most 8 MiB; use exports for large assets.",
            )
        digest = hashlib.sha256(raw).hexdigest()
        if "sha256" in reference and reference["sha256"] != digest:
            fail(
                "source_revision_conflict",
                "The pinned source hash differs from the current file; inspect the source before a new preflight.",
            )
        result.append(
            {
                "path": relative,
                "sha256": digest,
                **({"destination": destination} if "destination" in reference else {}),
            }
        )
    return result


def submit_request(args, workspace, *, allow_pinned_sources=False):
    arguments(args, SUBMIT_FIELDS)
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
    body = {**args, "gpu": boolean(args.get("gpu", False), "gpu")}
    if "workspace_files" in body:
        body["workspace_files"] = workspace_references(
            body["workspace_files"], workspace, allow_pinned_sources=allow_pinned_sources
        )
    if "gpu_memory_mb" in body:
        integer(body["gpu_memory_mb"], "gpu_memory_mb", 1, 2**31 - 1)
    if "retry_on_gpu_contention" in body:
        boolean(body["retry_on_gpu_contention"], "retry_on_gpu_contention")
    files = body.get("files", {})
    if not isinstance(files, dict) or len(files) > 128:
        fail("invalid_files", "Use a mapping of at most 128 relative source paths to UTF-8 text.")
    for name, content in files.items():
        relative_name(name)
        if not isinstance(content, str):
            fail("invalid_files", "Inline file content must be text; use exports for binary data.")
    references = body.get("workspace_files", [])
    destinations = [reference.get("destination", reference["path"]) for reference in references]
    names = set(files) | set(destinations)
    if len(names) != len(files) + len(destinations) or any(
        parent.as_posix() in names for name in names for parent in PurePosixPath(name).parents
    ):
        fail(
            "conflicting_job_files",
            "Use unique file destinations that do not conflict with directories.",
        )
    uploaded = sum(len(value.encode()) for value in files.values()) + sum(
        (workspace / reference["path"]).stat().st_size for reference in references
    )
    if len(names) > 128 or uploaded > 20 * 1024 * 1024:
        fail(
            "job_inputs_too_large", "Inline source must total at most 20 MiB; use exports for data."
        )
    exports = body.get("input_exports", [])
    if (
        not isinstance(exports, list)
        or len(exports) > 64
        or any(
            not isinstance(value, str) or not re.fullmatch(r"export-[a-f0-9]{32}", value)
            for value in exports
        )
    ):
        fail("invalid_input_exports", "Use at most 64 exact export IDs returned by the Host.")
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


def submission_result(result):
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


def dispatch(name, args, request, *, workspace=None):
    try:
        if name == "job_submit":
            return submission_result(
                request("POST", "/jobs", submit_request(args, Path(workspace or Path.cwd())))
            )
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
        if name in {"job_status", "job_cancel", "job_export"}:
            arguments(args, {"job_id", "path", "area"} if name == "job_export" else {"job_id"})
            path = "/jobs/" + identifier(args.get("job_id"))
            if name == "job_status":
                return request("GET", path)
            if name == "job_cancel":
                return request("DELETE", path)
            area = args.get("area", "outputs")
            if area not in {"workspace", "outputs"}:
                fail("invalid_area", "Use workspace or outputs.")
            return request(
                "POST",
                path + "/exports",
                {"location": area, "path": relative_name(args.get("path"))},
            )
        fail("unknown_job_tool", "Use one of the registered job tools.")
    except ToolInputError as error:
        return {"error": error.code, "hint": error.hint}
