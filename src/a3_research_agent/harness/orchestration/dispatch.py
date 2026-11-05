"""Worker coordination: dispatch."""

import json
import pathlib
from urllib.parse import urlsplit

from a3_research_agent.harness.context import manager as context_manager
from a3_research_agent.harness.orchestration.collection import (
    artifact,
    collect,
    collect_child_artifacts,
)
from a3_research_agent.harness.orchestration.common import checked, digest, is_main
from a3_research_agent.harness.orchestration.packages import prepared_worker_package, worker_package
from a3_research_agent.harness.orchestration.store import STATE, load, lookup_assignment, save, view


def dispatch(req, arguments):
    if not is_main(req):
        raise PermissionError("Only the current coordinator assigns and decides worker work")
    state = load()
    action = arguments.get("action")
    if action == "list":
        raw = STATE.read_bytes() if STATE.exists() else None
        if raw is not None:
            state = json.loads(raw)
            if state.get("version") != 1 or not isinstance(state.get("assignments"), dict):
                raise ValueError("Invalid assignment ledger; preserve and inspect it")
        source = {"path": str(STATE), "sha256": digest(raw) if raw is not None else None}
        identifier = arguments.get("id")
        if identifier is not None:
            return lookup_assignment(state, identifier)
        items = list(state["assignments"].values())
        pending = [x for x in items if x["status"] != "decided"]
        closed = [x for x in items if x["status"] == "decided"]
        selected = pending[:12] + closed[-6:]
        return {
            **view(state),
            "source": source,
            "detail": "Use delegate action=list with id for the complete unchanged record, or read the source ledger by pages. Reasons and evidence are omitted here, not deleted or reinterpreted.",
            "omitted_assignments": len(items) - len(selected),
            "assignments": [
                {
                    k: item.get(k)
                    for k in ("id", "role", "status", "session_id", "decision", "delivery_failed")
                }
                for item in selected
            ],
        }
    identifier = arguments.get("id")
    if action in {"assign", "assign_prepared"}:
        if identifier in state["assignments"]:
            raise ValueError("Assignment already recorded; inspect it, do not duplicate submission")
        while len(state["assignments"]) >= 64:
            archived = next(
                (
                    key
                    for key, value in state["assignments"].items()
                    if value["status"] == "decided"
                ),
                None,
            )
            if archived is None:
                raise ValueError("Too many unresolved assignments; close actual pending decisions")
            completed = state["assignments"][archived]
            raw = json.dumps(completed, ensure_ascii=False)
            context_manager.atomic(
                pathlib.Path("orchestration/archive")
                / (archived + "-" + digest(raw.encode()) + ".json"),
                raw,
            )
            state["assignments"].pop(archived)
            state["archived_count"] = state.get("archived_count", 0) + 1
        coordinator = checked(req, "GET", "/sessions/self").get("session_id")
        if not isinstance(coordinator, str) or not coordinator:
            raise ValueError("Cannot identify the coordinator for explicit export delivery")
        preparation = None
        if action == "assign_prepared":
            task, files, preparation = prepared_worker_package(state, arguments, coordinator)
        else:
            task, files = worker_package(arguments, coordinator_session_id=coordinator)
        item = {
            "id": identifier,
            "role": task["role"],
            "status": "creation_uncertain",
            "deliverables": task["deliverables"],
            "source_hashes": task["source_hashes"],
            "task_sha256": digest(json.dumps(task, sort_keys=True, ensure_ascii=False).encode()),
        }
        if preparation is not None:
            item["prepared_validation"] = preparation
        state["assignments"][identifier] = item
        save(state)  # Reserve before network I/O; uncertainty must not replay creation.
        result = checked(
            req,
            "POST",
            "/sessions",
            {
                "command": [
                    "python",
                    "-m",
                    "a3_research_agent.harness.agent",
                    "--resume",
                    "--worker-task",
                    "worker-task.json",
                ],
                "files": files,
                "purpose": "worker",
                "read_only_exports": task["read_only_exports"],
            },
        )
        if not isinstance(result.get("session_id"), str):
            raise ValueError("Missing worker identity; reconcile existing sessions")
        item.update(status="assigned", session_id=result["session_id"], ledger_persisted=True)
        try:
            save(state)
        except OSError:
            # The Host already created this identity. Preserve its real result;
            # the durable pre-POST reservation and task hash allow reconciliation.
            item["ledger_persisted"] = False
        return item
    if action == "reconcile" and identifier not in state["assignments"]:
        return lookup_assignment(state, identifier)
    if identifier not in state["assignments"]:
        raise ValueError("Unknown assignment")
    if action == "reconcile":
        item = state["assignments"][identifier]
        if item.get("session_id"):
            return item
        sessions = checked(req, "GET", "/sessions?include_status=true").get("sessions", [])
        matches = []
        for session in sessions:
            try:
                task = json.loads(artifact(req, session["session_id"], "worker-task.json"))
            except (ValueError, KeyError):
                continue
            if task.get("id") == identifier and digest(
                json.dumps(task, sort_keys=True, ensure_ascii=False).encode()
            ) == item.get("task_sha256"):
                matches.append(session["session_id"])
        if len(matches) > 1:
            raise ValueError("Ambiguous existing workers; preserve and inspect both")
        if matches:
            item.update(status="assigned", session_id=matches[0])
            save(state)
        return {**item, "creation_confirmed": bool(matches), "resubmitted": False}
    if action == "collect_artifacts":
        return collect_child_artifacts(req, state, arguments)
    if action == "collect":
        return collect(req, state, identifier)
    if action == "decide":
        item = state["assignments"][identifier]
        if item["status"] == "assigned" and arguments.get("decision") == "reject":
            worker = checked(req, "GET", "/sessions/" + item["session_id"])
            if worker.get("status") not in {"exited", "closed"}:
                raise ValueError("An active worker needs an explicit safe lifecycle decision first")
            item.update(
                status="awaiting_decision",
                delivery_failed=True,
                scientific_success=None,
                worker_status=worker.get("status"),
                evidence_hashes={},
                rejection_scope="Explicit rejection without validated delivery; experiment outcome unknown",
            )
        if item["status"] != "awaiting_decision" or arguments.get("decision") not in {
            "adopt",
            "reject",
            "follow_up",
        }:
            raise ValueError("Collect verified evidence before deciding")
        if item.get("delivery_failed") and arguments["decision"] == "adopt":
            raise ValueError("Cannot adopt a worker with unavailable delivery")
        reason = arguments.get("reason")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 4000:
            raise ValueError("A bounded decision reason is required")
        item.update(status="decided", decision=arguments["decision"], reason=reason)
        save(state)
        return item
    raise ValueError("Unknown delegation action")


def should_retire(req, task=None):
    if task is not None:
        return False
    own = checked(req, "GET", "/sessions/self")
    return own.get("has_been_main") is True and own.get("is_main") is False


def guard_gateway(req, method, path, body):
    method = method.upper()
    path = urlsplit(path).path.rstrip("/")
    if method == "POST" and path != "/sessions/self/stop" and should_retire(req):
        raise PermissionError("Former coordinator must save state and exit after handoff")
    adopting = (
        method == "POST"
        and path.startswith("/evolution/trials/")
        and path.endswith("/decision")
        and (body or {}).get("retain")
    )
    if not (method == "POST" and path == "/jobs") and not adopting:
        return
    if not is_main(req):
        return
    if not adopting:
        raise PermissionError("Coordinator must delegate Policy and Harness execution to workers")
    target = path.split("/")[3]
    sessions = checked(req, "GET", "/sessions?include_status=true").get("sessions")
    if not isinstance(sessions, list):
        raise TypeError("Unknown worker state; preserve existing ownership")
    active = []
    for record in sessions:
        if record.get("session_id") == target:
            continue
        status = record.get("status")
        status = status.get("status") if isinstance(status, dict) else status
        if status not in {"exited", "closed"}:
            active.append(record.get("session_id"))
    if active:
        raise ValueError("Settle existing workers before handoff; do not cancel or orphan them")
