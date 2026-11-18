"""High-level research operations over the fixed, authenticated Host API."""

import math
import re
from pathlib import PurePosixPath
from urllib.parse import urlencode

from .tool_contracts import (
    ToolInputError,
    arguments,
    definition,
    fail,
    integer,
    relative_name,
    text,
)

STRING = {"type": "string"}
JOB_ID = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
EXPORT_ID = {"type": "string", "pattern": "^export-[a-f0-9]{32}$"}
SESSION_ID = {"type": "string", "minLength": 1, "maxLength": 100}
AREA = {"type": "string", "enum": ["workspace", "outputs"]}
FRAME = {
    "type": "object",
    "properties": {
        "job_id": JOB_ID,
        "path": STRING,
        "area": AREA,
        "episode_id": STRING,
        "step": {"type": "integer", "minimum": 0},
        "sim_time_s": {"type": "number", "minimum": 0},
        "camera": STRING,
    },
    "required": ["job_id", "path", "episode_id", "step", "sim_time_s", "camera"],
    "additionalProperties": False,
}
FIELDS = {
    "vision_status": {},
    "vision_inspect": {
        "question": STRING,
        "frames": {"type": "array", "items": FRAME, "minItems": 1},
    },
    "evaluation_compare": {
        "candidate_job_id": JOB_ID,
        "baseline_job_id": JOB_ID,
        "baseline_manifest": STRING,
        "candidate_manifest": STRING,
    },
    "conditions_compare": {"baseline": {"type": "object"}, "candidate": {"type": "object"}},
    "benchmark_summary": {
        "reports": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {
                "type": "object",
                "properties": {"job_id": JOB_ID, "path": STRING},
                "required": ["job_id", "path"],
                "additionalProperties": False,
            },
        },
        "task": {"type": "string", "enum": ["2x5", "2x10"]},
    },
    "gpu_status": {},
    "runtime_status": {"scope": {"type": "string", "enum": ["self", "run"]}},
    "export_create": {"job_id": JOB_ID, "path": STRING, "area": AREA},
    "export_status": {"export_id": EXPORT_ID},
    "export_list": {},
    "export_manifest": {"export_id": EXPORT_ID},
    "export_delete": {"export_id": EXPORT_ID},
    "export_share": {"export_id": EXPORT_ID, "session_id": SESSION_ID},
    "export_revoke": {"export_id": EXPORT_ID, "session_id": SESSION_ID},
    "session_list": {},
    "session_status": {"session_id": SESSION_ID},
    "session_artifact": {
        "session_id": SESSION_ID,
        "path": STRING,
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 8000},
    },
    "session_stop": {"reason": STRING},
    "session_cancel": {"session_id": SESSION_ID},
}
DESCRIPTIONS = {
    "vision_status": "Check visual inspection availability and frame limits.",
    "vision_inspect": "Inspect authorized PNG/JPEG frames for a specific research question. Choose "
    "frames around the event and across cameras. Batch size is governed by image bytes, context "
    "budget and the provider. Supply real episode, step, simulation time and camera metadata. "
    "Group each episode contiguously in time order. area defaults outputs. Returns observations "
    "and source hashes.",
    "evaluation_compare": "Compare existing baseline/candidate episode results by job IDs and outputs-relative JSON manifests. Returns paired results and a ready-to-use visual inspection request when available.",
    "conditions_compare": "Compare declared baseline/candidate conditions. Each object includes status, report_present, and protocol(task,seeds,workers,step_budget,randomization,success_rule). Read actual results before judging improvement.",
    "benchmark_summary": "Combine the benchmark reports of one evaluation cell. Caps each episode at its own maximum, reports in-box counts and native passes separately, and names every protocol difference between segments (task, profile, step budget, action horizon, prompt style, checkpoint, duplicate seeds). Use it instead of adding report totals by hand.",
    "gpu_status": "Read aggregate available GPU capacity and utilization; the Controller selects actual devices.",
    "runtime_status": "Read model usage and runtime telemetry for self (default) or the current run.",
    "export_create": "Seal job files into a reusable read-only export. area defaults outputs. Creation is asynchronous; check export_status, then export_manifest.",
    "export_status": "Check whether a read-only export is preparing, ready or failed.",
    "export_list": "List exports accessible to this project identity, including authorized historical assets.",
    "export_manifest": "Read the paths, sizes and SHA256 hashes in a ready export.",
    "export_delete": "Delete an owned, unreferenced export; source job files are preserved.",
    "export_share": "Grant an export to an active session in this run, subject to Host ownership checks.",
    "export_revoke": "Revoke a current-run export grant; historical exports remain read-only.",
    "session_list": "List current-run research identities and their status.",
    "session_status": "Read current session status; omit session_id for self.",
    "session_artifact": "Read a character page from an authorized session file. Use conversation/... for its original context. Defaults limit=4000; returns whole-file SHA256 and next_offset.",
    "session_stop": "Stop this research process after saving plan, findings and evidence. The process may exit before receiving the reply.",
    "session_cancel": "Stop an owned session by ID, subject to the existing Host management permissions.",
}
REQUIRED = {
    "vision_inspect": ["question", "frames"],
    "evaluation_compare": [
        "candidate_job_id",
        "baseline_job_id",
        "baseline_manifest",
        "candidate_manifest",
    ],
    "conditions_compare": ["baseline", "candidate"],
    "export_create": ["job_id", "path"],
    **{name: ["export_id"] for name in ("export_status", "export_manifest", "export_delete")},
    **{name: ["export_id", "session_id"] for name in ("export_share", "export_revoke")},
    "benchmark_summary": ["reports"],
    "session_artifact": ["session_id", "path"],
    "session_stop": ["reason"],
    "session_cancel": ["session_id"],
}
TOOLS = [
    definition(name, DESCRIPTIONS[name], fields, REQUIRED.get(name, []))
    for name, fields in FIELDS.items()
]
NAMES = frozenset(FIELDS)


def identifier(value, field):
    pattern = (
        r"export-[a-f0-9]{32}"
        if field == "export_id"
        else r"[a-f0-9]{32}"
        if field.endswith("job_id")
        else r"[A-Za-z0-9_-]{1,100}"
    )
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        fail(
            "invalid_" + field,
            f"Use an exact {field} returned by the corresponding list or creation tool.",
        )
    return value


def area(value):
    if value not in {"outputs", "workspace"}:
        fail("invalid_area", "Choose outputs for persistent results or workspace for job files.")
    return value


def vision(arguments_value):
    text(arguments_value.get("question"), "question", 4000)
    frames = arguments_value.get("frames")
    if not isinstance(frames, list) or not frames:
        fail(
            "invalid_frames", "Supply frames with real episode, step, time and camera metadata."
        )
    normalized, seen, previous = [], set(), None
    for frame in frames:
        arguments(frame, FRAME["properties"])
        identifier(frame.get("job_id"), "job_id")
        path = relative_name(frame.get("path"))
        if not path.lower().endswith((".png", ".jpg", ".jpeg")):
            fail(
                "invalid_frame_path", "Select a PNG/JPEG file from the job's outputs or workspace."
            )
        text(frame.get("episode_id"), "episode_id", 100)
        text(frame.get("camera"), "camera", 64)
        integer(frame.get("step"), "step", 0, 2**63 - 1)
        seconds = frame.get("sim_time_s")
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            fail("invalid_sim_time_s", "Use the actual finite nonnegative simulation timestamp.")
        if previous is None or frame["episode_id"] != previous["episode_id"]:
            if frame["episode_id"] in seen:
                fail("invalid_frame_order", "Keep frames from each episode together in time order.")
            seen.add(frame["episode_id"])
        elif frame["step"] < previous["step"] or seconds < previous["sim_time_s"]:
            fail(
                "invalid_frame_order",
                "Keep each episode's steps and timestamps in increasing order.",
            )
        normalized.append({**frame, "area": area(frame.get("area", "outputs"))})
        previous = frame
    return {"question": arguments_value["question"], "frames": normalized}


def session_artifact_path(value):
    text(value, "path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or "\\" in value
        or any(part in {".", ".."} for part in path.parts)
        or any(ord(char) < 32 for char in value)
    ):
        fail("invalid_path", "Use a canonical session-relative file path without traversal.")
    return value


def dispatch(name, args, request):
    try:
        if name not in FIELDS:
            fail("unknown_host_tool", "Choose a registered high-level research tool.")
        arguments(args, FIELDS[name])
        if name in {"vision_status", "gpu_status", "export_list", "session_list"}:
            route = {
                "vision_status": "/vision/status",
                "gpu_status": "/resources/gpus",
                "export_list": "/jobs/exports",
                "session_list": "/sessions?include_status=true",
            }[name]
            return request("GET", route)
        if name == "vision_inspect":
            return request("POST", "/vision/inspect", vision(args))
        if name == "runtime_status":
            scope = args.get("scope", "self")
            if scope not in {"self", "run"}:
                fail("invalid_scope", "Choose self or run.")
            return request("GET", "/telemetry?" + urlencode({"scope": scope}))
        if name == "conditions_compare":
            if any(not isinstance(args.get(key), dict) for key in ("baseline", "candidate")):
                fail(
                    "invalid_conditions",
                    "Supply baseline and candidate condition objects from recorded results.",
                )
            return request("POST", "/checks/comparison", args)
        if name == "evaluation_compare":
            candidate = identifier(args.get("candidate_job_id"), "candidate_job_id")
            identifier(args.get("baseline_job_id"), "baseline_job_id")
            for field in ("baseline_manifest", "candidate_manifest"):
                if not relative_name(args.get(field), field).endswith(".json"):
                    fail("invalid_manifest", "Choose a JSON manifest relative to job outputs.")
            return request(
                "POST",
                f"/jobs/{candidate}/evaluation",
                {
                    key: args[key]
                    for key in ("baseline_job_id", "baseline_manifest", "candidate_manifest")
                },
            )
        if name == "benchmark_summary":
            from . import benchmark_report

            arguments(args, {"reports", "task"})
            return benchmark_report.summary(request, args)
        if name == "export_create":
            job = identifier(args.get("job_id"), "job_id")
            path = relative_name(args.get("path"))
            return request(
                "POST",
                f"/jobs/{job}/exports",
                {"path": path, "location": area(args.get("area", "outputs"))},
            )
        if name.startswith("export_"):
            export = identifier(args.get("export_id"), "export_id")
            route = f"/jobs/exports/{export}"
            if name in {"export_status", "export_manifest"}:
                return request("GET", route + ("/manifest" if name == "export_manifest" else ""))
            if name == "export_delete":
                return request("DELETE", route)
            session = identifier(args.get("session_id"), "session_id")
            return (
                request("POST", route + "/grants", {"session_id": session})
                if name == "export_share"
                else request("DELETE", route + "/grants/" + session)
            )
        if name == "session_stop":
            return request(
                "POST", "/sessions/self/stop", {"reason": text(args.get("reason"), "reason", 4000)}
            )
        session = identifier(args.get("session_id", "self"), "session_id")
        route = "/sessions/" + session
        if name == "session_status":
            return request("GET", route)
        if name == "session_cancel":
            return request("DELETE", route)
        path = session_artifact_path(args.get("path"))
        offset = integer(args.get("offset", 0), "offset", 0, 2**63 - 1)
        limit = integer(args.get("limit", 4000), "limit", 1, 8000)
        return request(
            "GET",
            route
            + "/artifacts?"
            + urlencode({"path": path, "offset": offset, "limit": limit, "text": "true"}),
        )
    except ToolInputError as error:
        return {"error": error.code, "hint": error.hint}
