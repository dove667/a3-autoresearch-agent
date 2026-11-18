"""Submit the two readiness checks before the Agent spends a turn on them.

A window used to open with the Agent discovering the dataset layout and the A3
callable surface by hand, which cost tens of minutes of model turns and, twice,
produced a wrong answer from an ad hoc probe. Both checks are one CPU job each, so
the Host submits them as soon as the session accepts work and records their IDs.

Every failure here is reported, never raised: a window must start even when the
Controller is unhappy.
"""

import base64
import json
from pathlib import Path

from a3_research_agent.harness import a3_api_runner, dataset_audit_runner
from a3_research_agent.harness.job_failure import diagnose
from a3_research_agent.host.sessions.workspace import write_durable

API_SCRIPT = "benchmarks/a3_api_reader.py"
DATASET_SCRIPT = "benchmarks/a3_dataset_audit_runner.py"
CHECKS_FILE = "startup-checks.json"
REPORTS = {"dataset_audit": "dataset-audit-startup/audit.json", "a3_api": "a3-api-startup/api.json"}
TERMINAL = {"completed", "failed", "cancelled"}
DATASET_REPORT_LIMIT = 256 * 1024


def _script(module):
    """Return the maintained launcher source; it travels inside the job request.

    The job carries the script as inline job input rather than a workspace
    attachment: attachments require a live research session record, and these
    checks are submitted before the Agent has one.
    """
    return Path(module.__file__).resolve().read_text(encoding="utf-8")


def _post(client, body):
    response = client.post("/jobs", json=body)
    if not hasattr(response, "raise_for_status"):
        return response if isinstance(response, dict) else {}
    response.raise_for_status()
    return response.json()


def submit(client, workspace, dataset_root=None):
    """Submit the API snapshot and, when configured, the dataset audit."""
    jobs, errors = [], []
    checks = [("a3_api", a3_api_runner, API_SCRIPT, ["--output-dir", "a3-api-startup"])]
    if dataset_root:
        checks.append(
            (
                "dataset_audit",
                dataset_audit_runner,
                DATASET_SCRIPT,
                ["--dataset-root", str(dataset_root), "--output-dir", "dataset-audit-startup"],
            )
        )
    for kind, module, name, arguments in checks:
        try:
            body = {
                "name": "startup-" + kind,
                "command": ["python", Path(name).name, *arguments],
                "gpu": False,
                "files": {Path(name).name: _script(module)},
            }
            result = _post(client, body)
            job_id = result.get("job_id") if isinstance(result, dict) else None
            if isinstance(job_id, str):
                jobs.append({"kind": kind, "job_id": job_id})
            else:
                errors.append(f"{kind}: no job_id in {json.dumps(result)[:200]}")
        except Exception as error:  # noqa: BLE001 - startup must survive a failed check
            errors.append(f"{kind}: {type(error).__name__}: {error}"[:300])
    return {"jobs": jobs, "errors": errors}


def record(run, result):
    """Keep the submitted checks next to the run so the Agent and the operator see them."""
    if not isinstance(result, dict):
        return
    try:
        write_durable(
            Path(run) / CHECKS_FILE,
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        )
    except OSError:
        pass


def load(run):
    """Resume observation of the original startup jobs."""
    try:
        saved = json.loads((Path(run) / CHECKS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"jobs": [], "errors": []}
    if not isinstance(saved, dict) or not isinstance(saved.get("jobs"), list):
        return {"jobs": [], "errors": []}
    saved["jobs"] = [
        job
        for job in saved["jobs"]
        if isinstance(job, dict)
        and isinstance(job.get("kind"), str)
        and job["kind"] in REPORTS
        and isinstance(job.get("job_id"), str)
    ]
    return saved


def dataset_result(response):
    if response.get("encoding") == "base64":
        data = base64.b64decode(response["data"], validate=True)
    elif response.get("encoding") == "utf-8" and response.get("eof", True):
        data = response["content"]
    else:
        raise ValueError("Startup dataset report is unavailable")
    if len(data) > DATASET_REPORT_LIMIT:
        raise ValueError("Startup dataset report exceeds the observation limit")
    report = json.loads(data)
    if (
        not isinstance(report, dict)
        or type(report.get("schema")) is not int
        or report["schema"] != 1
        or report.get("status") != "passed"
    ):
        raise ValueError("Invalid startup dataset report")
    metadata = report.get("metadata")
    if not isinstance(metadata, dict):
        raise TypeError("Startup dataset report has no metadata summary")
    return {
        **{key: metadata[key] for key in ("episodes", "layouts", "successful") if key in metadata},
        "warnings": report.get("warnings", []),
        "validation": report.get("validation", "a3_auditor"),
    }


def observe(run, checks, get):
    """Observe the startup jobs once without making readiness a launch dependency."""
    jobs = checks.get("jobs", [])
    changed = False
    for job in jobs:
        if job.get("status") in TERMINAL:
            continue
        try:
            identifier = job["job_id"]
            status = get(f"/jobs/{identifier}")
            if status.get("job_id") != identifier or status.get("status") not in TERMINAL:
                continue
        except Exception:  # noqa: BLE001, S112 - retry observation after temporary Gateway failures
            continue
        job.update(status=status["status"], exit_code=status.get("exit_code"))
        changed = True
        if problem := diagnose(status):
            job["failure"] = problem
            continue
        if status.get("exit_code") != 0:
            job["result_error"] = "Job did not report a successful exit."
            continue
        job["report"] = REPORTS[job["kind"]]
        if job["kind"] == "dataset_audit":
            try:
                job["result"] = dataset_result(
                    get(
                        f"/jobs/{identifier}/artifacts?area=outputs&path={job['report']}"
                        f"&max_bytes={DATASET_REPORT_LIMIT}"
                    )
                )
            except Exception as error:  # noqa: BLE001 - preserve terminal job evidence
                job["result_error"] = f"{type(error).__name__}: {error}"[:300]
    if changed:
        record(run, checks)
    return all(job.get("status") in TERMINAL for job in jobs)
