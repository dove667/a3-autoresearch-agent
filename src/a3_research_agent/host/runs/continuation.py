"""Start from an Agent's retained result, without a manually assembled handoff."""

import hashlib
import json
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from a3_research_agent.execution.exports.readonly import EXPORT_ID
from a3_research_agent.harness import state_paths
from a3_research_agent.host.published_harness import COMMAND, conversation_root, run_commit
from a3_research_agent.host.sessions.packages import load_published_manifest, read_continuation
from a3_research_agent.host.sessions.workspace import write_durable
from a3_research_agent.path_safety import workspace_path


@dataclass
class HarnessStart:
    command: list[str]
    files: dict[str, str]
    previous_run: Path | None = None
    remote_exports: list[str] = field(default_factory=list)
    source_owner: str | None = None
    harness_commit: str | None = None

    def __post_init__(self):
        if (
            self.command != COMMAND
            or not isinstance(self.harness_commit, str)
            or not re.fullmatch(r"[a-f0-9]{40}", self.harness_commit)
        ):
            raise ValueError(
                "A native research start requires the fixed published Harness command and commit"
            )

    @property
    def source_version(self):
        return self.harness_commit


def prepare_fresh_research(start: HarnessStart) -> tuple[HarnessStart, dict]:
    """Start an empty research context while preserving explicitly retained knowledge."""
    from a3_research_agent.host.runs.checkpoints import require_tool_results

    content = start.files.get(state_paths.CHECKPOINT)
    messages = json.loads(content) if isinstance(content, str) else None
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        raise ValueError("Published Harness needs a message-list checkpoint")
    require_tool_results(messages)
    if any(name != state_paths.CHECKPOINT and not _knowledge_name(name) for name in start.files):
        raise ValueError("Fresh research accepts only retained knowledge and a checkpoint")
    files = {
        **start.files,
        state_paths.CHECKPOINT: "[]",
    }
    return replace(start, files=files), {
        "mode": "fresh",
        "previous_message_count": len(messages),
        "source_checkpoint_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "initial_message_count": 0,
        "source_commit": start.harness_commit,
    }


def import_retained_exports(start: HarnessStart | None, recipient: str, remote_call) -> list[str]:
    """Authorize the explicitly selected retained data before a new main starts."""
    identifiers = start.remote_exports if start else []
    if not identifiers:
        return []
    if not start.source_owner:
        raise ValueError("Retained exports have no source owner")
    for offset in range(0, len(identifiers), 64):
        verified = remote_call(
            "exports-check",
            {"experiment_id": start.source_owner, "export_ids": identifiers[offset : offset + 64]},
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
    if not retained.is_file():
        raise ValueError("Previous run has no retained result")
    record = json.loads(retained.read_text())
    commit = run_commit(run)
    if (
        not isinstance(record, dict)
        or type(record.get("version")) is not int
        or record["version"] != 1
        or record.get("status") != "ready"
        or not commit
        or record.get("harness_commit") != commit
        or record.get("command") != COMMAND
    ):
        raise ValueError("Expected a ready retained result for the current published Harness")
    exports = _remote_exports(record.get("remote_exports"))
    files = {state_paths.CHECKPOINT: "[]", **_read_knowledge(run, record["knowledge"])}
    return HarnessStart(
        COMMAND.copy(),
        files,
        previous_run=run,
        remote_exports=exports,
        source_owner=record["selected_by"],
        harness_commit=commit,
    )


def resolve_harness_start(target: str | Path) -> HarnessStart:
    source = Path(target).resolve()
    if (source / "startup.json").is_file() or (source / "retained.json").is_file():
        return load_run_result(source)
    commit, command, files = load_published_manifest(source)
    return HarnessStart(command, files, harness_commit=commit)


def _remote_exports(identifiers):
    if (
        not isinstance(identifiers, list)
        or any(
            not isinstance(value, str) or not EXPORT_ID.fullmatch(value) for value in identifiers
        )
        or len(set(identifiers)) != len(identifiers)
    ):
        raise ValueError("Remote exports need a unique explicit export ID list")
    return identifiers.copy()


def _knowledge_name(name):
    parts = Path(name).parts
    return bool(
        parts
        and parts[0] in {"scripts", "experiments"}
        and not any(part.startswith(".") for part in parts)
    )


def _read_knowledge(run, reference):
    digest = reference.get("sha256") if isinstance(reference, dict) else None
    if (
        not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
        or reference.get("directory") != "retained-knowledge/" + digest
    ):
        raise ValueError("Invalid retained research knowledge reference")
    directory = workspace_path(run, reference["directory"])
    manifest = json.loads(workspace_path(directory, "manifest.json").read_text())
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"version", "files"}
        or type(manifest.get("version")) is not int
        or manifest["version"] != 1
        or not isinstance(manifest.get("files"), dict)
        or hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest() != digest
    ):
        raise ValueError("Retained research knowledge manifest changed")
    result = {}
    for name, expected in manifest["files"].items():
        if not _knowledge_name(name):
            raise ValueError("Unexpected file in retained research knowledge")
        content = workspace_path(directory, name).read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("Retained research knowledge content changed")
        result[name] = content.decode("utf-8")
    return result


def _retain_knowledge(run, workspace, names):
    content, workspace_binaries = {}, []
    for name in names:
        if not _knowledge_name(name):
            continue
        raw = workspace_path(workspace, name).read_bytes()
        if Path(name).parts[0] == "scripts":
            raw.decode("utf-8")
        else:
            try:
                raw.decode("utf-8")
                binary = b"\0" in raw
            except UnicodeDecodeError:
                binary = True
            if binary:
                workspace_binaries.append(
                    {
                        "path": name,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "bytes": len(raw),
                        "location": "shared_workspace",
                    }
                )
                continue
        content[name] = raw
    manifest = {
        "version": 1,
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(content.items())},
    }
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    reference = {"directory": "retained-knowledge/" + digest, "sha256": digest}
    directory = workspace_path(run, reference["directory"])
    directory.parent.mkdir(exist_ok=True)
    if not directory.exists():
        with tempfile.TemporaryDirectory(dir=directory.parent) as temporary:
            staging = Path(temporary) / "knowledge"
            staging.mkdir()
            for name, raw in content.items():
                target = workspace_path(staging, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            write_durable(staging / "manifest.json", json.dumps(manifest, sort_keys=True))
            staging.rename(directory)
    _read_knowledge(run, reference)
    return {**reference, "workspace_binary_files": workspace_binaries}


def publish_run_result(
    run: Path,
    workspace: Path,
    owner: str,
    *,
    validate_exports: Callable | None = None,
) -> dict:
    """Save the selected published source and immutable research knowledge."""
    declaration = state_paths.path(state_paths.CONTINUATION, conversation_root(run))
    if declaration.is_file():
        commit = run_commit(run)
        if commit is None:
            raise ValueError("Result retention requires a published Harness pin")
        manifest = read_continuation(workspace, conversation_root(run))
        remote_exports = _remote_exports(manifest.get("remote_exports", []))
        if remote_exports:
            if validate_exports is None:
                raise ValueError("Remote exports require Host access validation")
            validate_exports(remote_exports)
        result = {
            "status": "ready",
            "version": 1,
            "command": COMMAND.copy(),
            "harness_commit": commit,
            "selected_by": owner,
            "remote_exports": remote_exports,
            "knowledge": _retain_knowledge(run, workspace, manifest["files"]),
        }
    else:
        result = {
            "version": 1,
            "status": "missing",
            "reason": "Agent did not save continuation.json",
        }
    write_durable(run / "retained.json", json.dumps(result, indent=2))
    return result
