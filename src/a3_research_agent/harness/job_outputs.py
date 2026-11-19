"""Read job artifacts and list top-level output names without guessing.

The Host exposes a new list route (`/jobs/<id>/artifacts/list`) that older
deployments do not serve. Every helper here treats that absence as "unavailable"
instead of an error: a completed job must stay readable even while the Host and
the remote Controller are being upgraded at different times.
"""

import json
import re
from urllib.parse import urlencode

from . import tool_results
from .tool_contracts import arguments, fail, integer, relative_name

STRING = {"type": "string"}
ARTIFACT_FIELDS = {
    "job_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
    "path": STRING,
    "paths": {"type": "array", "items": STRING, "minItems": 1, "maxItems": 16},
    "directory": STRING,
    "area": {"type": "string", "enum": ["workspace", "outputs"]},
    "offset": {"type": "integer", "minimum": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 8000},
}
JOB_ID = re.compile(r"[a-f0-9]{32}")
MAX_LISTED = 200


def _names(values):
    """Keep only top-level names from a controller/fixture listing."""
    result = []
    for value in values:
        name = value.get("name") if isinstance(value, dict) else value
        if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name:
            continue
        if name not in result:
            result.append(name)
        if len(result) >= MAX_LISTED:
            break
    return result


def listing(request, job_id, area="outputs", directory=None):
    """Return artifact names, or an unavailable reason; never raise."""
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        return {"unavailable": "invalid_job_id"}
    if area not in {"workspace", "outputs"}:
        return {"unavailable": "invalid_area"}
    fields = {"area": area}
    if directory is not None:
        fields["directory"] = directory
    try:
        result = request("GET", f"/jobs/{job_id}/artifacts/list?" + urlencode(fields))
    except Exception:  # noqa: BLE001 - listing is optional evidence, not a decision
        return {"unavailable": "transport_error"}
    if not isinstance(result, dict):
        return {"unavailable": "invalid_response"}
    status = result.get("http_error")
    if isinstance(status, int):
        return {"unavailable": f"http_{status}"}
    if result.get("status") == "error":
        return {"unavailable": "controller_error"}
    entries = result.get("entries")
    if not isinstance(entries, list) or result.get("directory") != directory:
        return {"unavailable": "listing_unsupported"}
    return {"entries": _names(entries), **({"truncated": True} if result.get("truncated") else {})}


def directory_result(request, job_id, area, directory):
    result = listing(request, job_id, area, directory or None)
    source = {"job_id": job_id, "area": area, "directory": directory}
    if result.get("unavailable"):
        return {
            **source,
            **result,
            "error": "artifact_listing_unavailable",
            "hint": "Check that directory exists in this area and that the Controller supports directory listings.",
        }
    return {**source, **result}


def _detail(response):
    """Decode the Host's bounded error detail without forwarding private text."""
    if not isinstance(response, dict):
        return None
    try:
        body = json.loads(response.get("body") or "")
    except (TypeError, ValueError):
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, dict) else None


def _detail_names(detail):
    if not isinstance(detail, dict):
        return []
    for key in ("outputs", "entries", "files"):
        values = detail.get(key)
        if isinstance(values, list):
            return _names(values)
    return []


def _not_found(job_id, area, path, response, request, *, include_listing=False):
    detail = _detail(response)
    names = _detail_names(detail)
    if not names and include_listing:
        available = listing(request, job_id, area)
        names = available.get("entries", [])
    hint = (
        f"No {area} file at {path!r}. Call job_list to confirm the job ID, then use "
        f'job_artifact(area="{area}", directory="") for top-level names or directory=<relative path> '
        "for a subfolder. A missing file is not a reason to rerun the job."
    )
    if names:
        shown = ", ".join(names[:40])
        hint += f" Top-level names in {area}: {shown}"
        if len(names) > 40:
            hint += f", ... ({len(names)} total)"
    result = {
        "job_id": job_id,
        "area": area,
        "path": path,
        "error": "artifact_not_found",
        "hint": hint,
    }
    status = response.get("http_error") if isinstance(response, dict) else None
    if isinstance(status, int):
        result["http_status"] = status
    if names:
        result["outputs"] = names
    return result


def _page(job_id, path, area, offset, limit, request, *, list_when_missing=False):
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
    source = {"job_id": job_id, "area": area, "path": path}
    if not isinstance(result, dict) or result.get("http_error"):
        detail = _detail(result)
        code = detail.get("code") if isinstance(detail, dict) else None
        if code == "artifact_not_found":
            return _not_found(
                job_id, area, path, result, request, include_listing=list_when_missing
            )
        if code == "artifact_is_directory":
            return directory_result(request, job_id, area, path)
        return result
    encoding = result.get("encoding")
    if encoding == "base64":
        return {
            **source,
            "error": "artifact_pagination_unavailable",
            "hint": (
                "The Host or Controller deployment lacks artifact paging; report the "
                "version mismatch."
            ),
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


def read_batch(args, request):
    """Read one artifact page, or up to 16 named files from the same job and area."""
    arguments(args, {"job_id", "path", "paths", "directory", "area", "offset", "limit"})
    job_id = args.get("job_id")
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        fail(
            "invalid_job_id",
            "Use the exact 32-character job ID returned by job_submit or job_list.",
        )
    area = args.get("area", "workspace")
    if area not in {"workspace", "outputs"}:
        fail(
            "invalid_area", "Use workspace for submitted source or outputs for persistent results."
        )
    if "directory" in args:
        if args.keys() & {"path", "paths", "offset", "limit"}:
            fail(
                "invalid_artifact_paths",
                "Use directory for a listing without file or page arguments.",
            )
        directory = args["directory"]
        if directory != "":
            directory = relative_name(directory, "directory")
        return directory_result(request, job_id, area, directory)
    offset = integer(args.get("offset", 0), "offset", 0, 2**63 - 1)
    limit = integer(args.get("limit", 4000), "limit", 1, 8000)
    if "path" in args and "paths" in args:
        fail(
            "invalid_artifact_paths",
            "Pass either path for one file or paths for several, not both.",
        )
    if "paths" in args:
        values = args["paths"]
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 16
            or any(not isinstance(value, str) for value in values)
        ):
            fail(
                "invalid_artifact_paths",
                "Pass 1–16 relative artifact paths in paths.",
            )
        items = []
        for value in values:
            path = relative_name(value)
            items.append(tool_results.normalize(_page(job_id, path, area, offset, limit, request)))
        failed = [item for item in items if not item["ok"]]
        result = {
            "job_id": job_id,
            "area": area,
            "items": items,
            "count": len(items),
            "failed": len(failed),
        }
        if failed:
            result["notice"] = "Read every item; some artifact paths did not return a page."
            names = [name for item in failed for name in item.get("outputs", [])]
            if not names:
                available = listing(request, job_id, area)
                names = available.get("entries", [])
            names = _names(names)
            if names:
                result["outputs"] = names
            if len(failed) == len(items):
                result["error"] = "artifact_batch_failed"
                result["hint"] = (
                    "No path in this batch could be read. Use job_list to check the job or "
                    'job_artifact(directory="") to discover the output filenames.'
                )
    elif "path" not in args:
        fail("missing_artifact_path", "Pass path for one artifact or paths for several.")
    else:
        path = relative_name(args.get("path"))
        result = _page(job_id, path, area, offset, limit, request, list_when_missing=True)
    return result
