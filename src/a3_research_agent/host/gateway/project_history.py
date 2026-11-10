"""Index research scopes from private Host checkpoints in the same project."""

import json
from pathlib import Path


def project_job_index(current_run: Path | None) -> tuple[dict[str, str], dict[str, str]]:
    if current_run is None:
        return {}, {}

    index = current_run.parent.parent / "project-job-index.json"
    if current_run.parent.name == "rsi" and index.is_file() and not index.is_symlink():
        saved = json.loads(index.read_text())
        scope_runs = {
            scope: run for scope, run in saved["scope_runs"].items() if run != current_run.name
        }
        job_scopes = {
            identifier: scope
            for identifier, scope in saved["job_scopes"].items()
            if scope in scope_runs
        }
    else:
        scope_runs, job_scopes = {}, {}
    for run in sorted(current_run.parent.glob("run-*"), reverse=True):
        if run == current_run or run.is_symlink():
            continue
        state_path = run / ".host/state.json"
        if state_path.parent.is_symlink() or state_path.is_symlink():
            continue
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            continue
        if state.get("run_id") != run.name:
            continue
        gateway = state.get("gateway", {})
        scopes = set(gateway.get("root_owners", [])) | gateway.get("children", {}).keys()
        for scope in sorted(scopes, key=lambda owner: (owner == run.name, owner)):
            if scope in scope_runs and scope_runs[scope] != run.name:
                continue
            scope_runs[scope] = run.name
            for identifier in gateway.get("jobs", {}).get(scope, []):
                job_scopes[identifier] = scope
    return scope_runs, job_scopes
