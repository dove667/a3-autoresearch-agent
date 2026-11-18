"""Aggregate A3 benchmark reports by cell, with the protocol kept honest.

A cell is usually four jobs of five episodes each. Summing their scores by hand
miscounts: ``cookies_in_target`` can exceed the task maximum (an 11/10 episode was
counted as eleven), and segments with different step budgets, action horizons or
checkpoints look comparable in a flat table. This module reads the reports,
caps each episode at its own maximum, separates in-box counts from native passes,
and reports every difference it finds in the protocol.
"""

import json
from urllib.parse import urlencode

from .tool_contracts import fail, relative_name

PAGE_LIMIT = 8000
REPORT_BYTES = 262144
MAX_EPISODES = 60
TASK_MAX = {"2x5": 10, "2x10": 20}
IDENTITY_FIELDS = ("task", "profile", "max_steps", "n_action_steps", "prompt_style")


def read_report(request, job_id, path):
    """Read one UTF-8 report through the artifact pages, then parse it."""
    offset, pieces = 0, []
    while True:
        page = request(
            "GET",
            f"/jobs/{job_id}/artifacts?"
            + urlencode({"area": "outputs", "path": path, "offset": offset, "limit": PAGE_LIMIT}),
        )
        if not isinstance(page, dict) or page.get("http_error"):
            fail(
                "benchmark_report_unreadable",
                f"Report {path} could not be read; check the job and path with job_list first.",
            )
        if page.get("encoding") != "utf-8" or not isinstance(page.get("content"), str):
            fail("benchmark_report_not_text", f"Report {path} is not a UTF-8 JSON file.")
        pieces.append(page["content"])
        total = page.get("bytes")
        if type(total) is int and total > REPORT_BYTES:
            fail("benchmark_report_too_large", f"Report {path} exceeds {REPORT_BYTES} bytes.")
        if page.get("eof") or not page.get("content"):
            break
        next_offset = page.get("end")
        if type(next_offset) is not int or next_offset <= offset:
            fail("benchmark_report_stalled", f"Report {path} did not advance past {offset}.")
        offset = next_offset
    try:
        report = json.loads("".join(pieces))
    except ValueError:
        fail("benchmark_report_invalid_json", f"Report {path} is not complete JSON.")
    if not isinstance(report, dict) or not isinstance(report.get("episodes"), list):
        fail("benchmark_report_invalid", f"Report {path} has no episodes array.")
    return report


def episode_row(episode, fallback_max):
    if not isinstance(episode, dict):
        return None
    score = episode.get("cookies_in_target", episode.get("score"))
    maximum = episode.get("max_score", fallback_max)
    if type(score) is not int or type(maximum) is not int or maximum <= 0:
        return None
    return {
        "seed": episode.get("seed"),
        "score": score,
        "max_score": maximum,
        "capped": min(score, maximum),
        "success": bool(episode.get("success")),
        "steps": episode.get("steps"),
    }


def summarise(request, reports, task=None):
    """Combine reports from one cell and name every protocol difference."""
    rows, identities, checkpoints, paths = [], {field: set() for field in IDENTITY_FIELDS}, set(), []
    for entry in reports:
        job = entry["job_id"]
        path = entry["path"]
        report = read_report(request, job, path)
        run = report.get("run") if isinstance(report.get("run"), dict) else {}
        fallback = TASK_MAX.get(str(run.get("task") or task or "2x5"), 10)
        for field in IDENTITY_FIELDS:
            if run.get(field) is not None:
                identities[field].add(str(run[field]))
        if run.get("weights_sha256"):
            checkpoints.add(str(run["weights_sha256"])[:12])
        for episode in report["episodes"]:
            row = episode_row(episode, fallback)
            if row is not None:
                rows.append({**row, "job_id": job})
        paths.append(path)
    if not rows:
        fail("benchmark_report_empty", "These reports contain no scored episode.")
    warnings = []
    for field, values in identities.items():
        if len(values) > 1:
            warnings.append(f"mixed {field}: " + ", ".join(sorted(values)))
    if len(checkpoints) > 1:
        warnings.append("mixed checkpoint: " + ", ".join(sorted(checkpoints)))
    seeds = [row["seed"] for row in rows if row["seed"] is not None]
    if len(seeds) != len(set(seeds)):
        warnings.append("duplicate seeds across reports: sum would double-count episodes")
    maximum = sum(row["max_score"] for row in rows)
    in_box = sum(row["score"] for row in rows)
    capped = sum(row["capped"] for row in rows)
    if in_box > maximum:
        warnings.append(
            f"raw in-box {in_box} exceeds the {maximum} the task allows; "
            f"report the capped {capped} for comparisons"
        )
    passes = sum(1 for row in rows if row["success"])
    distribution = {}
    for row in rows:
        key = str(row["score"])
        distribution[key] = distribution.get(key, 0) + 1
    return {
        "reports": len(paths),
        "episodes": len(rows),
        "max_possible": maximum,
        "in_box_raw": in_box,
        "in_box": capped,
        "in_box_rate": round(capped / maximum, 4) if maximum else 0.0,
        "passed": passes,
        "pass_rate": round(passes / len(rows), 4),
        "zero_score": sum(1 for row in rows if row["score"] == 0),
        "score_distribution": dict(sorted(distribution.items(), key=lambda item: int(item[0]))),
        "mean_steps": round(
            sum(row["steps"] for row in rows if type(row["steps"]) is int) / max(1, sum(
                1 for row in rows if type(row["steps"]) is int
            )),
            1,
        ),
        "protocol": {field: sorted(values) for field, values in identities.items() if values},
        "checkpoints": sorted(checkpoints),
        "warnings": warnings,
        "episodes_detail": [
            {key: row[key] for key in ("job_id", "seed", "score", "capped", "success", "steps")}
            for row in rows[:MAX_EPISODES]
        ],
    }


def reports_from(args):
    """Validate the reports argument: one or more job outputs-relative JSON paths."""
    entries = args.get("reports")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
        fail("invalid_reports", "Supply 1–16 {job_id, path} entries.")
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"job_id", "path"}:
            fail("invalid_reports", "Each entry needs only job_id and path.")
        job = entry["job_id"]
        if not isinstance(job, str) or len(job) != 32 or any(c not in "0123456789abcdef" for c in job):
            fail("invalid_job_id", "Use an exact 32-character job_id.")
        result.append({"job_id": job, "path": relative_name(entry.get("path"))})
    return result


def summary(request, args):
    reports = reports_from(args)
    task = args.get("task")
    if task is not None and task not in TASK_MAX:
        fail("invalid_task", "Use task 2x5 or 2x10, or omit it to take it from the reports.")
    return summarise(request, reports, task)

