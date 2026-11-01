"""Start from an Agent's retained result, without a manually assembled handoff."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from runtime.local.candidate_runtime import candidate_workspace
from runtime.local.candidate_versions import CandidateVersions
from runtime.local.harness_package import load_harness_package


@dataclass
class HarnessStart:
    command: list[str]
    files: dict[str, str]
    previous_run: Path | None = None
    source_version: str | None = None


def prepare_fresh_task(start: HarnessStart, task: str) -> tuple[HarnessStart, dict]:
    """Initialize a copied message checkpoint; preserve the retained code and memory."""
    content = start.files.get("checkpoint.json")
    if content is None:
        raise ValueError("Fresh tasks require a supported checkpoint.json message list")
    messages = json.loads(content)
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        raise ValueError("Unsupported checkpoint format; Harness needs an explicit new-task entry")
    files = dict(start.files)
    files["checkpoint.json"] = json.dumps([{"role": "user", "content": task}], ensure_ascii=False)
    return (
        HarnessStart(start.command.copy(), files, start.previous_run, start.source_version),
        {
            "mode": "fresh",
            "previous_message_count": len(messages),
            "source_checkpoint_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "initial_message_count": 1,
        },
    )


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

    raise ValueError("Previous run has no retained result; refusing to guess or rebuild")


def resolve_harness_start(target: str | Path) -> HarnessStart:
    source = Path(target).resolve()
    if (source / "startup.json").is_file() or (source / "retained.json").is_file():
        return load_run_result(source)
    command, files = load_harness_package(source)
    return HarnessStart(command, files)


def resolve_resume_start(run: Path) -> HarnessStart | None:
    startup = json.loads((run / "startup.json").read_text())
    main_file = run / "evolution.json"
    main_owner = (
        json.loads(main_file.read_text()).get("main_owner") if main_file.is_file() else None
    )
    if main_owner and main_owner != startup.get("run_id"):
        session_id = main_owner
    elif startup.get("mode") == "retained-harness":
        session_id = startup["session_id"]
    else:
        return None
    workspace = candidate_workspace(run / "candidates", session_id)
    if not (workspace / "resume.json").is_file():
        raise ValueError(
            "Harness has no resume.json checkpoint entry; refusing a fresh-context restart"
        )
    command, files = load_harness_package(workspace, "resume.json")
    return HarnessStart(command, files, run, startup.get("version_id"))


def publish_run_result(
    run: Path, workspace: Path, versions: CandidateVersions, owner: str, parent_version: str | None
) -> dict:
    """Persist the Agent's explicit selection; never rank candidate versions here."""
    declaration = workspace / "continuation.json"
    if declaration.is_file():
        command, files = load_harness_package(workspace, "continuation.json")
        version = versions.create_local(owner, files, parent_version)
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
