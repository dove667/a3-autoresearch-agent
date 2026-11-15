"""Resume the current published native run from its explicit Host checkpoint."""

import hashlib
import json
import os
import re
import secrets
import time
from copy import deepcopy
from pathlib import Path

from a3_research_agent.host.published_harness import COMMAND, run_commit, run_identity
from a3_research_agent.host.runs.directories import debug_directory
from a3_research_agent.host.runs.state import RunStateStore
from a3_research_agent.host.sessions.workspace import (
    exit_archived,
    require_native_record,
    session_workspace,
    write_durable,
)
from a3_research_agent.workspace.paths import workspace_path


def load_resume_state(run: Path, store: RunStateStore, model: str) -> dict:
    if not store.path.is_file():
        raise ValueError("Native resume requires the saved Host checkpoint")
    state = store.load()
    if (
        not isinstance(state, dict)
        or type(state.get("version")) is not int
        or state["version"] != 1
        or state.get("backend") != "process"
    ):
        raise ValueError("Unsupported native Host checkpoint schema")
    if state.get("run_id") != run_identity(run) or state.get("model") != model:
        raise ValueError("Resume must retain the saved run and model")
    if state.get("status") == "completed":
        raise ValueError("This run has ended; select its retained Harness for a new run")
    commit = run_commit(run)
    if commit is None or state.get("harness_commit") != commit:
        raise ValueError("Host checkpoint must match the current published Harness pin")
    startup = json.loads(workspace_path(run, "startup.json").read_text())
    source = startup.get("harness_commit") if isinstance(startup, dict) else None
    if (
        not isinstance(startup, dict)
        or startup.get("run_id") != run_identity(run)
        or not isinstance(source, str)
        or not re.fullmatch(r"[a-f0-9]{40}", source)
    ):
        raise ValueError("Invalid original native startup provenance")
    gateway = state.get("gateway", {})
    actor = gateway.get("main_owner") if isinstance(gateway, dict) else None
    if (
        not isinstance(actor, str)
        or not isinstance(gateway.get("session_records"), dict)
        or actor not in gateway["session_records"]
    ):
        raise ValueError("Current research identity is missing from the Host checkpoint")
    record = json.loads(workspace_path(run, f"sessions/{actor}/session.json").read_text())
    require_native_record(record)
    if record.get("owner") != actor or record.get("session_id") != actor:
        raise ValueError("Native checkpoint session identity does not match its owner")
    session_workspace(run / "sessions", actor)
    return state


def preflight_blocked_resume(
    run: Path, state: dict, *, feedback: dict | None = None
) -> dict | None:
    """Read-only eligibility check for an explicit CLI resume, never supervisor retry."""
    from a3_research_agent.host.runs.supervisor import evaluate_outcome

    journal = run / ".host/blocked-resume"
    for path in journal.glob("*/attempt.json"):
        if json.loads(path.read_text()).get("status") != "complete":
            raise ValueError(
                f"Blocked resume pending at {path.parent}; inspect the process and archived "
                "state before recovery; automatic retry is disabled"
            )
    gateway = state["gateway"]
    actor = gateway["main_owner"]
    child = gateway["session_records"].get(actor)
    path = workspace_path(run, f"sessions/{actor}/session.json")
    if child is None or not path.is_file():
        return None
    record_text = path.read_text()
    record = json.loads(record_text)
    require_native_record(record)
    if not (
        child.get("self_stopped") or record.get("self_stop_requested") or record.get("self_stopped")
    ):
        return None
    if (
        state.get("status") not in {"suspended", "stopped"}
        or child.get("closed") is not False
        or record.get("closed") is not False
        or record.get("owner") != actor
        or record.get("session_id") != actor
        or child.get("owner") != actor
        or record.get("self_stop_requested") is not True
        or record.get("self_stopped") is not True
        or record.get("launch_pending") is not False
        or not exit_archived(record)
    ):
        raise ValueError("Blocked resume requires the confirmed stopped, nonclosed current main")
    workspace = session_workspace(run / "sessions", actor)
    outcome = evaluate_outcome(workspace, ())
    supervision = json.loads(workspace_path(run, "supervision.json").read_text())
    host_outcome = json.loads(workspace_path(run, "outcome.json").read_text())
    prior_attempt = None
    if not (
        outcome.get("status") == "blocked"
        and host_outcome.get("status") == "blocked"
        and supervision.get("phase") == "stopped"
        and supervision.get("outcome", {}).get("status") == "blocked"
    ):
        prior_attempt = _feedback_retry(run, workspace, actor, feedback, host_outcome, supervision)
        if prior_attempt != child.get("restart_attempt"):
            raise ValueError("Workspace terminal does not match the Host restart attempt")
    commit = run_commit(run)
    if commit is None or state.get("harness_commit") != commit or record.get("command") != COMMAND:
        raise ValueError("Blocked resume requires the fixed current published Harness")
    checkpoint_text = workspace_path(workspace, "checkpoint.json").read_text()
    messages = json.loads(checkpoint_text)
    if (
        not isinstance(messages, list)
        or not messages
        or not all(isinstance(item, dict) for item in messages)
    ):
        raise ValueError("Blocked resume requires a nonempty message checkpoint")
    _validate_blocked_checkpoint(messages)
    snapshots = {
        "session.json": record_text,
        "run-outcome.json": workspace_path(workspace, "run-outcome.json").read_text(),
        "outcome.json": workspace_path(run, "outcome.json").read_text(),
        "supervision.json": workspace_path(run, "supervision.json").read_text(),
    }
    console = workspace_path(debug_directory(run), f"{actor}/console.log")
    if console.is_file():
        snapshots["console.log"] = console.read_text()
    return {
        "actor": actor,
        "feedback": feedback,
        "prior_attempt": prior_attempt,
        "workspace": workspace,
        "record_text": record_text,
        "snapshots": snapshots,
        "harness_commit": commit,
        "command": COMMAND.copy(),
        "checkpoint_sha256": hashlib.sha256(checkpoint_text.encode()).hexdigest(),
    }


def _feedback_retry(run, workspace, actor, feedback, host_outcome, supervision):
    """Only reopen the stopped result of a verified prior blocked restart, with new notice."""
    from a3_research_agent.host.runs.supervisor import EXTERNAL_BLOCKERS, evidence_exists

    error = (
        "Self-stopped main needs valid blocked evidence or an audited resume with explicit feedback"
    )
    current = json.loads(workspace_path(workspace, "run-outcome.json").read_text())
    previous = current.get("previous_terminal")
    if (
        not feedback
        or feedback.get("owner") != actor
        or not isinstance(previous, str)
        or current.get("status") != "continue"
        or current.get("reason") != "Explicit operator resume"
        or host_outcome.get("status") != "incomplete"
        or host_outcome.get("reason") != "Explicit operator resume"
        or supervision.get("phase") != "stopped"
        or supervision.get("outcome") != host_outcome
    ):
        raise ValueError(error)
    attempt = workspace_path(run, previous)
    if attempt.parent != run / ".host/blocked-resume":
        raise ValueError(error)
    metadata = json.loads(workspace_path(attempt, "attempt.json").read_text())
    if (
        metadata.get("status") != "complete"
        or metadata.get("main_owner") != actor
        or metadata.get("run_id") != run_identity(run)
        or metadata.get("event") != "explicit_blocked_resume"
    ):
        raise ValueError(error)
    hashes = metadata.get("previous_sha256", {})
    required = {"session.json", "run-outcome.json", "outcome.json", "supervision.json"}
    if not required <= hashes.keys() or not hashes.keys() <= required | {"console.log"}:
        raise ValueError(error)
    for name, expected in hashes.items():
        content = workspace_path(attempt, name).read_text()
        if hashlib.sha256(content.encode()).hexdigest() != expected:
            raise ValueError("Prior blocked resume archive hash mismatch")
    prior = json.loads(workspace_path(attempt, "run-outcome.json").read_text())
    record = json.loads(workspace_path(attempt, "session.json").read_text())
    root = json.loads(workspace_path(attempt, "outcome.json").read_text())
    supervisor = json.loads(workspace_path(attempt, "supervision.json").read_text())
    blocker = prior.get("external_blocker", {})
    if (
        prior.get("status") != "blocked"
        or root.get("status") != "blocked"
        or supervisor.get("phase") != "stopped"
        or supervisor.get("outcome", {}).get("status") != "blocked"
        or record.get("owner") != actor
        or record.get("closed") is not False
        or not isinstance(blocker, dict)
        or blocker.get("kind") not in EXTERNAL_BLOCKERS
        or blocker.get("requires_human") is not True
        or not all(
            isinstance(blocker.get(key), str) and blocker[key].strip()
            for key in ("reason", "required_action")
        )
        or not evidence_exists(workspace, blocker.get("evidence"))
    ):
        raise ValueError(error)
    return previous


def _validate_blocked_checkpoint(messages: list[dict]) -> None:
    from a3_research_agent.host.runs.checkpoints import require_tool_results

    require_tool_results(messages)
    calls, results = set(), set()
    for message in messages:
        for item in [message]:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind not in {"function_call", "function_call_output"}:
                continue
            identifier = item.get("call_id")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("Checkpoint tool call needs a stable call_id")
            if kind == "function_call":
                if identifier in calls:
                    raise ValueError("Duplicate checkpoint tool call")
                calls.add(identifier)
            else:
                if identifier not in calls or identifier in results:
                    raise ValueError("Unpaired or duplicate checkpoint tool result")
                results.add(identifier)
    if calls != results:
        raise ValueError("Unresolved tool execution in checkpoint")


def begin_blocked_resume(
    run, state, store, runtime, plan, input_manifest, feedback_registration=None
):
    """Durably journal the authorization before changing current terminal state.

    The caller holds RunLease. A pending journal intentionally fences off all later
    CLI attempts: a crash can occur after a process starts but before its receipt saves.
    """
    # Recheck before credential issuance, including checkpoint/outcome changes since preflight.
    current = preflight_blocked_resume(run, state, feedback=plan.get("feedback"))
    if current != plan:
        raise ValueError("Blocked resume source changed after preflight")
    actor = plan["actor"]
    record = runtime.records[actor]
    if record != json.loads(plan["record_text"]):
        raise ValueError("Blocked resume runtime state changed after preflight")
    if plan.get("feedback") and (
        not feedback_registration
        or feedback_registration.get("sha256") != plan["feedback"]["sha256"]
    ):
        raise ValueError("Resume notification must be registered through HumanFeedback first")
    attempt = run / ".host/blocked-resume" / str(time.time_ns())
    attempt.mkdir(parents=True)
    write = write_durable
    metadata = {
        "status": "pending",
        "event": "explicit_blocked_resume",
        "run_id": run_identity(run),
        "main_owner": actor,
        "input_manifest": input_manifest,
        "feedback": feedback_registration,
        "prior_attempt": plan.get("prior_attempt"),
        "checkpoint_sha256": plan["checkpoint_sha256"],
        "harness_commit": plan["harness_commit"],
        "command": plan["command"],
        "previous_sha256": {
            name: hashlib.sha256(text.encode()).hexdigest()
            for name, text in plan["snapshots"].items()
        },
    }
    # Journal first: partial archival also fails closed without issuing any new token.
    write(attempt / "attempt.json", json.dumps(metadata, indent=2))
    for name, text in plan["snapshots"].items():
        write(attempt / name, text)
    write(
        attempt / "gateway-child.json",
        json.dumps(state["gateway"]["session_records"][actor], indent=2),
    )
    logs = debug_directory(run)
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (logs / "session-events.jsonl").open("a") as stream:
        stream.write(
            json.dumps({**metadata, "attempt": str(attempt.relative_to(run)), "time": time.time()})
            + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())
    continuing = json.dumps(
        {
            "status": "continue",
            "reason": "Explicit operator resume",
            "previous_terminal": str(attempt.relative_to(run)),
            "input_manifest": input_manifest,
        },
        indent=2,
    )
    write(plan["workspace"] / "run-outcome.json", continuing)
    write(run / "outcome.json", continuing)
    restarted = deepcopy(record)
    restarted.update(
        self_stopped=False, self_stop_requested=False, restart_attempt=str(attempt.relative_to(run))
    )
    runtime.save_record(restarted)
    runtime.records[actor] = restarted
    gateway = state["gateway"]
    child = gateway["session_records"][actor]
    # Revoke every stale credential for only this identity, retaining all other scopes.
    gateway["sessions"] = {
        token: owner for token, owner in gateway["sessions"].items() if owner != actor
    }
    child.update(
        token=secrets.token_urlsafe(32),
        self_stopped=False,
        restart_attempt=str(attempt.relative_to(run)),
    )
    gateway["sessions"][child["token"]] = actor
    store.update(gateway=gateway)
    return attempt


def finish_blocked_resume(attempt: Path) -> None:
    path = attempt / "attempt.json"
    metadata = json.loads(path.read_text())
    metadata["status"] = "complete"
    write_durable(path, json.dumps(metadata, indent=2))
