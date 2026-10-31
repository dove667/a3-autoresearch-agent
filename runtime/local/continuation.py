"""Start from an Agent's retained result, without a manually assembled handoff."""

import json
from dataclasses import dataclass
from pathlib import Path

from runtime.local.candidate_runtime import workspace_path
from runtime.local.candidate_versions import CandidateVersions
from runtime.local.harness_package import load_harness_package


@dataclass
class HarnessStart:
    command: list[str]
    files: dict[str, str]
    previous_run: Path | None = None
    source_version: str | None = None


def load_run_result(run: Path) -> HarnessStart:
    retained = run / "retained.json"
    if retained.is_file():
        record = json.loads(retained.read_text())
        if record.get("status") != "ready":
            raise ValueError(
                "Previous run has no retained result; Agent must save continuation.json"
            )
        files = CandidateVersions.read_snapshot(run / "versions", record["version_id"])
        command = record["command"]
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(x, str) for x in command)
        ):
            raise ValueError("Invalid retained command")
        return HarnessStart(command, files, run, record["version_id"])

    # Compatibility with the existing cold start's final output convention.
    final = workspace_path(run, "workspace/run/final")
    if (final / "run.json").is_file():
        command, files = load_harness_package(final)
        return HarnessStart(command, files, run)
    if (final / "entry.py").is_file() and not (final / "entry.py").is_symlink():
        return HarnessStart(
            ["python", "entry.py"], {"entry.py": (final / "entry.py").read_bytes().decode()}, run
        )
    raise ValueError("Previous run has no retained result; refusing to guess or rebuild")


def resolve_harness_start(target: str | Path) -> HarnessStart:
    source = Path(target).resolve()
    if (source / "startup.json").is_file() or (source / "retained.json").is_file():
        return load_run_result(source)
    command, files = load_harness_package(source)
    return HarnessStart(command, files)


def resolve_resume_start(run: Path) -> HarnessStart | None:
    startup = json.loads((run / "startup.json").read_text())
    if startup.get("mode") != "retained-harness":
        return None
    if (run / "retained.json").is_file():
        retained = json.loads((run / "retained.json").read_text())
        if retained.get("status") == "ready":
            return load_run_result(run)
    version_id = startup["version_id"]
    files = CandidateVersions.read_snapshot(run / "versions", version_id)
    metadata = workspace_path(run, f"candidates/{startup['session_id']}/session.json")
    command = json.loads(metadata.read_text())["command"]
    return HarnessStart(command, files, run, version_id)


def publish_run_result(
    run: Path, workspace: Path, versions: CandidateVersions, owner: str, parent_version: str | None
) -> dict:
    """Persist the Agent's explicit selection; never rank candidate versions here."""
    declaration = workspace / "continuation.json"
    if declaration.is_file():
        command, files = load_harness_package(workspace, "continuation.json")
        version = versions.create(owner, files, parent_version)
        result = {
            "status": "ready",
            "command": command,
            "version_id": version["version_id"],
            "commit": version["commit"],
            "selected_by": owner,
        }
    else:
        result = {"status": "missing", "reason": "Agent did not save continuation.json"}
    temporary = run / "retained.tmp"
    temporary.write_text(json.dumps(result, indent=2))
    temporary.replace(run / "retained.json")
    return result
