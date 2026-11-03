"""Start from an Agent's retained result, without a manually assembled handoff."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
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
    remote_exports: list[str] = field(default_factory=list)
    source_owner: str | None = None


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
    # Operational progress and analyzer cadence belong to this task, not to the
    # inherited research capabilities. Preserve them as history in the new copy.
    reset = {
        "compaction-result.json": None,
        "compaction-epoch.json": {"version": 1, "id": None},
        "orchestration/assignments.json": {"version": 1, "assignments": {}},
        "operational-state.json": {
            "version": 1,
            "revision": 0,
            "reads": {},
            "items": {},
            "counters": {},
        },
        "operational-state.json.previous": {
            "version": 1,
            "revision": 0,
            "reads": {},
            "items": {},
            "counters": {},
        },
        "analysis-client/schedule.handoff.json": {"id": None},
        "analysis-client/schedule.json": {
            "version": 1,
            "last_poll": None,
            "last_attempt": None,
            "last_periodic": None,
            "samples": [],
            "latest": None,
        },
    }
    replacements = {name: json.dumps(value) for name, value in reset.items()}
    replacements["history.jsonl"] = ""
    removed = {"context-history/compaction-progress.json", "context-failure.json"}
    present = {
        name: start.files[name] for name in set(replacements) | removed if name in start.files
    }
    identity = json.dumps([start.source_version, task, present], sort_keys=True)
    prefix = (
        "inherited-task-state/operational/" + hashlib.sha256(identity.encode()).hexdigest() + "/"
    )
    operational_archives = {}
    for name, inherited_content in present.items():
        target = prefix + name
        if target in files and files[target] != inherited_content:
            raise ValueError("Operational task archive collision")
        files[target] = inherited_content
        if name in removed:
            files.pop(name, None)
        else:
            files[name] = replacements[name]
        operational_archives[name] = target
    if operational_archives:
        for name in ("resume.json", "continuation.json"):
            if name not in files:
                continue
            declaration = json.loads(files[name])
            if not isinstance(declaration, dict) or not isinstance(declaration.get("files"), list):
                raise TypeError("Invalid inherited operational file declaration")
            declaration["files"] = sorted(
                (set(declaration["files"]) - removed) | set(operational_archives.values())
            )
            files[name] = json.dumps(declaration)

    return (
        replace(start, command=start.command.copy(), files=files),
        {
            "mode": "fresh",
            "previous_message_count": len(messages),
            "source_checkpoint_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "initial_message_count": 1,
            "operational_task_archives": operational_archives,
        },
    )


def import_retained_exports(start: HarnessStart | None, recipient: str, remote_call) -> list[str]:
    """Authorize the explicitly selected retained data before a new main starts."""
    identifiers = start.remote_exports if start else []
    if not identifiers:
        return []
    if not start.source_owner:
        raise ValueError("Retained exports have no source owner")
    verified = remote_call(
        "exports-check", {"experiment_id": start.source_owner, "export_ids": identifiers}
    )
    if verified.get("status") == "error":
        raise ValueError("Retained exports are not accessible or ready")
    granted = []
    try:
        for identifier in identifiers:
            result = remote_call(
                "export-grant",
                {
                    "experiment_id": start.source_owner,
                    "export_id": identifier,
                    "recipient": recipient,
                },
            )
            if result.get("status") == "error":
                raise ValueError("Cannot import a retained read-only export")
            granted.append(identifier)
    except Exception:
        for identifier in granted:
            remote_call(
                "export-revoke",
                {
                    "experiment_id": start.source_owner,
                    "export_id": identifier,
                    "recipient": recipient,
                },
            )
        raise
    return identifiers.copy()


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
        return HarnessStart(
            command,
            files,
            run,
            record["version_id"],
            record.get("remote_exports", []),
            record.get("selected_by"),
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
    run: Path,
    workspace: Path,
    versions: CandidateVersions,
    owner: str,
    parent_version: str | None,
    *,
    validate_exports: Callable | None = None,
) -> dict:
    """Persist the Agent's explicit selection; never rank candidate versions here."""
    declaration = workspace / "continuation.json"
    if declaration.is_file():
        command, files = load_harness_package(workspace, "continuation.json")
        remote_exports = json.loads(declaration.read_text()).get("remote_exports", [])
        if not isinstance(remote_exports, list) or any(
            not isinstance(x, str) for x in remote_exports
        ):
            raise ValueError("remote_exports must be an explicit export ID list")
        if remote_exports:
            if validate_exports is None:
                raise ValueError("Remote exports require Host access validation")
            validate_exports(remote_exports)
        version = versions.create_local(owner, files, parent_version)
        result = {
            "status": "ready",
            "command": command,
            "version_id": version["version_id"],
            "commit": version["commit"],
            "selected_by": owner,
            "remote_exports": remote_exports,
        }
    else:
        result = {"status": "missing", "reason": "Agent did not save continuation.json"}
    temporary = run / "retained.tmp"
    temporary.write_text(json.dumps(result, indent=2))
    temporary.replace(run / "retained.json")
    return result
