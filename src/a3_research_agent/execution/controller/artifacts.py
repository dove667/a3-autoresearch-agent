"""Read bounded owner-scoped workspace or persistent output artifacts."""

import base64
import codecs
import hashlib
import os

from a3_research_agent.execution.exports.readonly import open_directory, open_source

from .files import MAX_FILE_BYTES, safe_path
from .store import JobStore


def read_text_page(stream, offset: int, limit: int, max_bytes: int) -> dict:
    """Hash one fixed descriptor while retaining only the requested UTF-8 characters."""
    before = os.fstat(stream.fileno())
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")()
    byte_count, total, pieces, binary = 0, 0, [], False
    while True:
        chunk = stream.read(min(65536, max_bytes - byte_count + 1))
        byte_count += len(chunk)
        if byte_count > max_bytes:
            raise ValueError("artifact exceeds size limit")
        digest.update(chunk)
        if not binary:
            try:
                text = decoder.decode(chunk, final=not chunk)
            except UnicodeDecodeError:
                binary, pieces = True, []
            else:
                left = max(0, offset - total)
                right = min(len(text), offset + limit - total)
                if right > left:
                    pieces.append(text[left:right])
                total += len(text)
        if not chunk:
            break
    after = os.fstat(stream.fileno())
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if identity(before) != identity(after):
        raise ValueError("artifact changed during paged read; retry its current version")
    source = {"bytes": byte_count, "sha256": digest.hexdigest()}
    if binary:
        return {**source, "encoding": "binary"}
    start, end = min(offset, total), min(offset + limit, total)
    return {
        **source,
        "encoding": "utf-8",
        "unit": "characters",
        "offset": start,
        "end": end,
        "total": total,
        "eof": end == total,
        "content": "".join(pieces),
    }


def read_artifact(store: JobStore, request: dict) -> dict:
    job = store.load(request.get("job_id"), request.get("experiment_id"))
    limit = request.get("max_bytes", MAX_FILE_BYTES)
    if type(limit) is not int or not 0 < limit <= MAX_FILE_BYTES:
        raise ValueError("Invalid artifact byte ceiling")
    paged = "offset" in request or "limit" in request
    if paged and (
        type(request.get("offset")) is not int
        or request["offset"] < 0
        or type(request.get("limit")) is not int
        or not 1 <= request["limit"] <= 8000
    ):
        raise ValueError("Artifact page requires nonnegative offset and limit in 1..8000")
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
        if paged:
            return {
                "path": request["path"],
                "area": area,
                **read_text_page(stream, request["offset"], request["limit"], limit),
            }
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


def list_artifacts(store: JobStore, request: dict) -> dict:
    """List names in an owner-scoped directory without reading file contents."""
    job = store.load(request.get("job_id"), request.get("experiment_id"))
    area = request.get("area", "workspace")
    if area not in {"workspace", "outputs"}:
        raise ValueError("Unknown artifact area")
    if area == "outputs" and not job.get("run_id"):
        raise ValueError("outputs area is unavailable for legacy jobs")
    root = store.outputs(job) if area == "outputs" else store.workspace(job)
    directory = request.get("directory")
    target = root if directory is None else safe_path(root, directory)
    try:
        with open_directory(target) as descriptor:
            names = sorted(os.listdir(descriptor))
    except FileNotFoundError:
        if directory is not None:
            raise
        names = []
    return {
        "area": area,
        **({"directory": directory} if directory is not None else {}),
        "entries": names[:200],
        "count": min(len(names), 200),
        "truncated": len(names) > 200,
    }
