"""Submit the two readiness checks before the Agent spends a turn on them.

A window used to open with the Agent discovering the dataset layout and the A3
callable surface by hand, which cost tens of minutes of model turns and, twice,
produced a wrong answer from an ad hoc probe. Both checks are one CPU job each, so
the Host submits them as soon as the session accepts work and records their IDs.

Every failure here is reported, never raised: a window must start even when the
Controller is unhappy.
"""

import hashlib
import json
from pathlib import Path

from a3_research_agent.harness import a3_api_runner, dataset_audit_runner

API_SCRIPT = "benchmarks/a3_api_reader.py"
DATASET_SCRIPT = "benchmarks/a3_dataset_audit_runner.py"


def _install(workspace, module, name):
    """Write a maintained launcher into the workspace and return its reference."""
    directory = Path(workspace) / "benchmarks"
    directory.mkdir(parents=True, exist_ok=True)
    source = Path(module.__file__).resolve().read_text(encoding="utf-8")
    target = directory / Path(name).name
    target.write_text(source, encoding="utf-8")
    return {
        "path": str(Path(name)),
        "sha256": hashlib.sha256(source.encode()).hexdigest(),
        "destination": Path(name).name,
    }


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
            reference = _install(workspace, module, name)
            body = {
                "name": "startup-" + kind,
                "command": ["python", Path(name).name, *arguments],
                "gpu": False,
                "workspace_files": [reference],
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
        (Path(run) / "startup-checks.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass
