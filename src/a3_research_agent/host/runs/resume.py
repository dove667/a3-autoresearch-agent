"""Validate in-place recovery and import Host metadata from pre-checkpoint runs."""

import hashlib
import json
import os
import secrets
import socket
import time
from copy import deepcopy
from pathlib import Path

from a3_research_agent.host.runs.state import RunStateStore
from a3_research_agent.host.sessions.containers import workspace_path


def original_model(run: Path) -> str:
    seen = set()
    while run not in seen:
        seen.add(run)
        startup = json.loads((run / "startup.json").read_text())
        if startup.get("model"):
            return startup["model"]
        parent = startup.get("parent_run")
        if not parent:
            break
        run = workspace_path(run.parent, parent)
    raise ValueError("Cannot verify the original model for this checkpoint")


def load_resume_state(
    run: Path, store: RunStateStore, model: str, model_class: str | None = None
) -> dict:
    if store.path.is_file():
        state = store.load()
        if state["run_id"] != run.name or state["model"] != model:
            raise ValueError("Resume must retain the original run and model")
        if model_class is not None and state.get("model_class") != model_class:
            raise ValueError("Resume requires the original model adapter")
        if state.get("status") == "completed":
            raise ValueError("This run has ended; select its retained Harness for a new run")
        return state
    if original_model(run) != model:
        raise ValueError("Resume requires the original model")
    startup = json.loads((run / "startup.json").read_text())
    if startup.get("run_id") != run.name:
        raise ValueError("Resume requires the original run directory name")
    evolution_file = run / "evolution.json"
    evolution = (
        json.loads(evolution_file.read_text())
        if evolution_file.exists()
        else {
            "main_owner": startup.get("session_id", run.name),
            "trials": {},
            "transitions": [],
        }
    )
    children, parents, sessions = {}, {}, {secrets.token_urlsafe(32): run.name}
    events = run / "session-events.jsonl"
    for line in events.read_text().splitlines() if events.exists() else []:
        event = json.loads(line)
        if event["event"] != "created":
            continue
        session = event["session_id"]
        path = workspace_path(run, f"candidates/{session}/session.json")
        record = json.loads(path.read_text())
        if not record["closed"]:
            raise ValueError("Stop the old Host before importing its session metadata")
        if record["owner"] != session or event["parent"] not in {run.name, *children}:
            raise ValueError("Invalid saved Host session relationship")
        token = secrets.token_urlsafe(32)
        closed = record["closed"] and session != evolution["main_owner"]
        children[session] = {
            "parent": event["parent"],
            "owner": session,
            "token": token,
            "closed": closed,
            "version_id": record.get("version_id"),
        }
        parents[session] = event["parent"]
        if not closed:
            sessions[token] = session
    if evolution["main_owner"] != run.name and evolution["main_owner"] not in children:
        raise ValueError("Current main identity is missing from Host metadata")
    state = {
        "run_id": run.name,
        "model": model,
        "model_class": model_class,
        "status": "suspended",
        "endpoint": json.loads((run / "inputs/deployment.json").read_text())["gateway"],
        "gateway": {
            "sessions": sessions,
            "root_owners": [run.name],
            "children": children,
            "scope_parents": parents,
            "jobs": {},
            "evolution": evolution,
            "fixtures": {},
        },
        "imported_host_metadata": True,
    }
    # Old authentication material was not persisted. Rotate tokens while preserving scopes.
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", int(state["endpoint"].rsplit(":", 1)[1])))
    store.save(state)
    return state


def preflight_blocked_resume(
    run: Path, state: dict, *, feedback: dict | None = None
) -> dict | None:
    """Read-only eligibility check for an explicit CLI resume, never supervisor retry."""
    from a3_research_agent.host.runs.supervisor import evaluate_outcome
    from a3_research_agent.host.sessions.containers import candidate_workspace
    from a3_research_agent.host.sessions.packages import load_harness_package

    journal = run / ".host/blocked-resume"
    for path in journal.glob("*/attempt.json"):
        if json.loads(path.read_text()).get("status") != "complete":
            raise ValueError(
                f"Blocked resume pending at {path.parent}; inspect the container and archived "
                "state before recovery; automatic retry is disabled"
            )
    gateway = state["gateway"]
    actor = gateway["evolution"]["main_owner"]
    child = gateway["children"].get(actor)
    path = workspace_path(run, f"candidates/{actor}/session.json")
    if child is None or not path.is_file():
        return None
    record_text = path.read_text()
    record = json.loads(record_text)
    if not (
        child.get("self_stopped") or record.get("self_stop_requested") or record.get("self_stopped")
    ):
        return None
    evolution = json.loads(workspace_path(run, "evolution.json").read_text())
    if evolution.get("main_owner") != actor:
        raise ValueError("Saved main identity disagrees with the evolution history")
    terminal = record.get("terminal_state", {})
    if (
        state.get("status") not in {"suspended", "stopped"}
        or child.get("closed") is not False
        or record.get("closed") is not False
        or record.get("owner") != actor
        or record.get("session_id") != actor
        or child.get("owner") != actor
        or record.get("self_stop_requested") is not True
        or record.get("self_stopped") is not True
        or record.get("container_removed") is not True
        or terminal.get("Status") not in {"exited", "dead"}
        or any(terminal.get(key) is not False for key in ("Running", "Paused", "Restarting"))
    ):
        raise ValueError("Blocked resume requires the confirmed stopped, nonclosed current main")
    workspace = candidate_workspace(run / "candidates", actor)
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
    command, files = load_harness_package(workspace, "resume.json")
    if (
        len(command) != 3
        or command[0] not in {"python", "python3"}
        or command[2] != "--resume"
        or command[1] not in files
        or not command[1].endswith(".py")
        or "checkpoint.json" not in files
    ):
        raise ValueError("Blocked resume requires an explicit Python checkpoint resume command")
    checkpoint = json.loads(files["checkpoint.json"])
    messages = checkpoint.get("messages") if isinstance(checkpoint, dict) else checkpoint
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
    console = workspace_path(run, f"candidates/{actor}/console.log")
    if console.is_file():
        snapshots["console.log"] = console.read_text()
    return {
        "actor": actor,
        "feedback": feedback,
        "prior_attempt": prior_attempt,
        "workspace": workspace,
        "record_text": record_text,
        "snapshots": snapshots,
        "resume_sha256": hashlib.sha256(
            workspace_path(workspace, "resume.json").read_bytes()
        ).hexdigest(),
        "source_sha256": {
            name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()
        },
        "command": command,
        "checkpoint_sha256": hashlib.sha256(
            workspace_path(workspace, "checkpoint.json").read_bytes()
        ).hexdigest(),
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
        or metadata.get("run_id") != run.name
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
    from a3_research_agent.host.runs.context import require_tool_results

    require_tool_results(messages)
    calls, results = set(), set()
    for message in messages:
        items = message.get("output")
        items = items if isinstance(items, list) else [message]
        for item in items:
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
    CLI attempts: a crash can occur after Docker starts but before its receipt saves.
    """
    from a3_research_agent.host.sessions.containers import CandidateRuntime

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
    write = CandidateRuntime.write_durable
    metadata = {
        "status": "pending",
        "event": "explicit_blocked_resume",
        "run_id": run.name,
        "main_owner": actor,
        "input_manifest": input_manifest,
        "feedback": feedback_registration,
        "prior_attempt": plan.get("prior_attempt"),
        "checkpoint_sha256": plan["checkpoint_sha256"],
        "resume_sha256": plan["resume_sha256"],
        "source_sha256": plan["source_sha256"],
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
    write(attempt / "gateway-child.json", json.dumps(state["gateway"]["children"][actor], indent=2))
    with (run / "session-events.jsonl").open("a") as stream:
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
    child = gateway["children"][actor]
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
    from a3_research_agent.host.sessions.containers import CandidateRuntime

    path = attempt / "attempt.json"
    metadata = json.loads(path.read_text())
    metadata["status"] = "complete"
    CandidateRuntime.write_durable(path, json.dumps(metadata, indent=2))
