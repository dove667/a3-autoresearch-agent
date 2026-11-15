"""Apply published maintenance releases to suspended research workspaces."""

import hashlib
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path

from a3_research_agent.harness import state_paths
from a3_research_agent.host.inputs.feedback import HumanFeedback
from a3_research_agent.host.inputs.versions import InputVersions
from a3_research_agent.host.models.settings import model_settings
from a3_research_agent.host.published_harness import (
    COMMAND,
    MARKER,
    conversation_root,
    native_source,
    project_for_run,
    replace_run_pin,
    run_commit,
    run_identity,
)
from a3_research_agent.host.runs.checkpoints import require_tool_results
from a3_research_agent.host.runs.state import RunLease, RunStateStore
from a3_research_agent.host.sessions.processes import ProcessRuntime
from a3_research_agent.host.sessions.workspace import (
    exit_archived,
    session_workspace,
    write_durable,
)


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode()


def checkpoint_boundary(state_root: Path) -> dict[str, bytes]:
    """Validate recovery structure; pending operations remain journaled, never executed."""
    checkpoint = state_paths.path(state_paths.CHECKPOINT, state_root).read_bytes()
    messages = json.loads(checkpoint)
    if not isinstance(messages, list) or any(not isinstance(row, dict) for row in messages):
        raise ValueError("Published release requires a message-list checkpoint")
    require_tool_results(messages)
    boundary = {state_paths.CHECKPOINT: checkpoint}
    pending = state_paths.path(state_paths.JOURNAL, state_root)
    if pending.is_file():
        raw = pending.read_bytes()
        journal = json.loads(raw)
        boundary[state_paths.JOURNAL] = raw
        if journal is not None:
            if (
                not isinstance(journal, dict)
                or not isinstance(journal.get("output"), list)
                or not isinstance(journal.get("results"), dict)
            ):
                raise ValueError("Invalid pending tool journal; reconcile before release")
            if any(not isinstance(row, dict) for row in journal["output"]):
                raise ValueError("Invalid pending tool output; reconcile before release")
            calls = [row for row in journal["output"] if row.get("type") == "function_call"]
            expected = {row.get("call_id") for row in calls}
            if None in expected or len(expected) != len(calls):
                raise ValueError("Invalid pending tool identities; reconcile before release")
            if hashlib.sha256(checkpoint).hexdigest() != journal.get("base_hash"):
                delivered = {
                    row.get("call_id")
                    for row in messages
                    if row.get("type") == "function_call_output"
                }
                if not expected <= delivered:
                    raise ValueError("Tool journal/checkpoint mismatch; reconcile before release")
    return boundary


def archive_once(path: Path, content: bytes) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        write_durable(path, content.decode("utf-8"))


def apply_release(
    run: Path, directory: Path, digest: str, *, environment: Mapping[str, str] | None = None
) -> dict:
    """Upgrade source/config under the run lease, preserving its research identity."""
    from a3_research_agent.host.release_packages import verify_release

    run, directory = run.resolve(), directory.resolve()
    root = project_for_run(run)
    settings = model_settings(os.environ if environment is None else environment)
    verify_release(directory, digest)
    package = json.loads((directory / "package/run.json").read_text())
    commit = package.get("harness_commit")
    if commit is None:
        raise ValueError("In-place release requires a published Harness commit")
    if (
        package.get("version") != 1
        or package.get("command") != COMMAND
        or package.get("files") != [state_paths.CHECKPOINT]
        or json.loads((directory / "package" / state_paths.CHECKPOINT).read_text()) != []
    ):
        raise ValueError("Published release must select the fixed source entry, not research state")
    source = native_source(root, commit)
    identifier = hashlib.sha256(
        encoded({"release": digest, "model_settings": settings})
    ).hexdigest()
    archive = run / ".host/release-upgrades" / identifier
    pending_path = run / ".host/pending-release.json"
    with RunLease(root / ".runtime/research-workspace"), RunLease(run):
        store = RunStateStore(run)
        state = store.load()
        if state.get("run_id") != run_identity(run) or state.get("status") != "suspended":
            raise ValueError("In-place release requires the original suspended run")
        if state.get("version") != 1 or state.get("backend") != "process":
            raise ValueError("Release requires a current native run")
        completed = archive / "result.json"
        if (
            state.get("release_upgrade_id") == identifier
            and completed.is_file()
            and not pending_path.exists()
        ):
            return json.loads(completed.read_text())
        previous = run_commit(run)
        if previous is None:
            raise ValueError("Only existing published Harness workspaces support in-place releases")
        actor = state["gateway"]["main_owner"]
        child = state["gateway"].get("session_records", {}).get(actor)
        if actor == run_identity(run) or not child or child.get("closed"):
            raise ValueError("Release requires the current nonclosed research identity")
        session_workspace(run / "sessions", actor)
        checkpoint_boundary(conversation_root(run))
        contents = {
            name: (directory / "inputs" / name).read_text()
            for name in json.loads((directory / "manifest.json").read_text())["inputs"]
            if name != "initial-feedback.md"
        }
        release_inputs = InputVersions.from_proposal(
            root,
            run,
            {
                "contents": contents,
                "files": {name: {"source": str(directory / "inputs" / name)} for name in contents},
            },
        )
        proposal = release_inputs.prepare()
        pending = json.loads(pending_path.read_text()) if pending_path.exists() else None
        if pending is not None:
            if pending.get("upgrade_id") != identifier or previous not in {
                pending["previous_commit"],
                commit,
            }:
                raise ValueError("Another release is pending; inspect its archived state")
        else:
            archive.mkdir(parents=True, mode=0o700, exist_ok=True)
            archive_once(archive / "state-before.json", store.path.read_bytes())
            archive_once(archive / "pin-before.json", (run / MARKER).read_bytes())
            deployment = run / "inputs/deployment.json"
            if deployment.is_file():
                archive_once(archive / "deployment-before.json", deployment.read_bytes())
            archive_once(
                archive / "input-manifest-before.json",
                (run / "inputs/input-manifest.json").read_bytes(),
            )
            record_path = run / "sessions" / actor / "session.json"
            archive_once(archive / "session-before.json", record_path.read_bytes())
            pending = {
                "upgrade_id": identifier,
                "release_digest": digest,
                "previous_commit": previous,
                "commit": commit,
                "main_owner": actor,
                "status": "prepared",
            }
            write_durable(pending_path, encoded(pending).decode())
        runtime = ProcessRuntime(
            root=run / "sessions",
            inputs=run / "inputs",
            public_a3=run / "public/a3",
            endpoint=state["endpoint"],
            published_source=source,
        )
        runtime.load_records()
        for session, other in runtime.records.items():
            if (
                session != actor
                and not other.get("closed")
                and not exit_archived(other)
                and runtime.state(other)["status"] in {"created", "running", "restarting", "paused"}
            ):
                raise ValueError("Release requires all other research sessions to be stopped")
        runtime.prepare_release_restart(actor)
        boundary = checkpoint_boundary(conversation_root(run))
        for name, raw in boundary.items():
            target = archive / name
            if target.exists() and target.read_bytes() != raw:
                raise ValueError("Research checkpoint changed during a pending release")
            archive_once(target, raw)
        current = json.loads((run / "inputs/input-manifest.json").read_text())
        if current.get("digest") != proposal["digest"]:
            versions = InputVersions.from_proposal(root, run, proposal)
            current = versions.activate(proposal, "release")
        # The release note is delivered through the feedback protocol.
        (run / "inputs/initial-feedback.md").unlink(missing_ok=True)
        if run_commit(run) != commit:
            replace_run_pin(run, pending["previous_commit"], commit)
        feedback_path = directory / "inputs/initial-feedback.md"
        feedback = (
            HumanFeedback(run).register_content(feedback_path.read_bytes(), actor)
            if feedback_path.is_file()
            else None
        )
        store.update(
            **settings,
            harness_commit=commit,
            release_upgrade_id=identifier,
            release_digest=digest,
            status="suspended",
        )
        result = {
            "status": "complete",
            "upgrade_id": identifier,
            "release_digest": digest,
            "run_id": run_identity(run),
            "main_owner": actor,
            "previous_commit": pending["previous_commit"],
            "commit": commit,
            "input_revision": current["revision"],
            "model_settings": settings,
            "feedback": feedback,
            "time": time.time(),
        }
        write_durable(completed, encoded(result).decode())
        with (run / ".host/release-events.jsonl").open("a") as stream:
            stream.write(encoded(result).decode() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        pending_path.unlink()
        return result
