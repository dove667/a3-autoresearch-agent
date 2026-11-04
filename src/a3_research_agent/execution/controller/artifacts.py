"""Read bounded owner-scoped workspace or persistent output artifacts."""

import base64
import os

from a3_research_agent.execution.exports.readonly import open_source

from .files import MAX_FILE_BYTES, safe_path
from .store import JobStore


def read_artifact(store: JobStore, request: dict) -> dict:
    job = store.load(request.get("job_id"), request.get("experiment_id"))
    limit = request.get("max_bytes", MAX_FILE_BYTES)
    if type(limit) is not int or not 0 < limit <= MAX_FILE_BYTES:
        raise ValueError("Invalid artifact byte ceiling")
    area = request.get("area", "workspace")
    if area not in {"workspace", "outputs"}:
        raise ValueError("Unknown artifact area")
    if area == "outputs" and not job.get("run_id"):
        raise ValueError("outputs area is unavailable for legacy jobs")
    root = store.outputs(job) if area == "outputs" else store.workspace(job)
    target = safe_path(root, request.get("path"))
    # Fixed directory descriptors prevent a symlink swap after lexical checks.
    # The optional ceiling only narrows the existing owner-scoped read boundary.
    with open_source(target) as stream:
        if os.fstat(stream.fileno()).st_size > limit:
            raise ValueError("artifact exceeds size limit")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("artifact exceeds size limit")
    result = {
        "path": request["path"],
        "encoding": "base64",
        "data": base64.b64encode(data).decode(),
    }
    if area == "outputs":
        result["area"] = area
    return result
