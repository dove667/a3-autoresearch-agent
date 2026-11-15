"""Index research scopes from private Host checkpoints in the same project."""

import json
from pathlib import Path

from a3_research_agent.host.published_harness import run_identity


def project_job_index(current_run: Path | None) -> tuple[dict[str, str], dict[str, str]]:
    if current_run is None:
        return {}, {}

    index = current_run.parent.parent / ".runtime/project-job-index.json"
    if current_run.parent.name == "trajectories" and index.is_file() and not index.is_symlink():
        saved = json.loads(index.read_text())
        identity = run_identity(current_run)
        scope_runs = {scope: run for scope, run in saved["scope_runs"].items() if run != identity}
        job_scopes = {
            identifier: scope
            for identifier, scope in saved["job_scopes"].items()
            if scope in scope_runs
        }
    else:
        scope_runs, job_scopes = {}, {}
    for run in sorted(current_run.parent.glob("*"), reverse=True):
        if run == current_run or run.is_symlink() or not run.is_dir():
            continue
        state_path = run / ".host/state.json"
        if state_path.parent.is_symlink() or state_path.is_symlink():
            continue
        try:
            state = json.loads(state_path.read_text())
            identity = run_identity(run)
        except (OSError, ValueError):
            continue
        if (
            state.get("run_id") != identity
            or state.get("version") != 1
            or state.get("backend") != "process"
        ):
            continue
        gateway = state.get("gateway", {})
        scopes = set(gateway.get("root_owners", [])) | gateway.get("session_records", {}).keys()
        for scope in sorted(scopes, key=lambda owner: (owner == identity, owner)):
            if scope in scope_runs and scope_runs[scope] != identity:
                continue
            scope_runs[scope] = identity
            for identifier in gateway.get("jobs", {}).get(scope, []):
                job_scopes[identifier] = scope
    return scope_runs, job_scopes
