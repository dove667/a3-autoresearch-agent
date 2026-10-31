"""Validate in-place recovery and import Host metadata from pre-checkpoint runs."""

import json
import secrets
import socket
from pathlib import Path

from runtime.local.candidate_runtime import workspace_path
from runtime.local.run_state import RunStateStore


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
