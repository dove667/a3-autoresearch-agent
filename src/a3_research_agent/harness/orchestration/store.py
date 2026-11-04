"""Worker coordination: store."""

import json
import pathlib
import re

from a3_research_agent.harness.context import manager as context_manager
from a3_research_agent.harness.orchestration.common import digest
from a3_research_agent.harness.orchestration.packages import (
    bounded_preparation_bytes,
    strict_package_json,
)

STATE = pathlib.Path("orchestration/assignments.json")


def load():
    value = json.loads(STATE.read_text()) if STATE.exists() else {"version": 1, "assignments": {}}
    if value.get("version") != 1 or not isinstance(value.get("assignments"), dict):
        raise ValueError("Invalid assignment ledger; preserve and inspect it")
    return value


def save(value):
    context_manager.atomic(STATE, json.dumps(value, ensure_ascii=False))


def view(state=None):
    state = load() if state is None else state
    items = list(state["assignments"].values())
    pending = [item for item in items if item["status"] != "decided"]
    return {
        "scope": "this_task",
        "pending": [
            {k: item.get(k) for k in ("id", "role", "status", "session_id", "delivery_failed")}
            for item in pending[:12]
        ],
        "omitted_pending": max(0, len(pending) - 12),
        "retained_assignments": len(items),
        "archived_assignments": state.get("archived_count", 0),
        "note": "Recorded operational assignments, not scientific acceptance. Use delegate collect/decide for evidence and decisions.",
    }


def lookup_assignment(state, identifier):
    """Describe ledger absence, never infer whether an external POST succeeded."""
    if not isinstance(identifier, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier
    ):
        raise ValueError("Invalid assignment lookup identity")
    raw = STATE.read_bytes() if STATE.exists() else None
    source = {"path": str(STATE), "sha256": digest(raw) if raw is not None else None}
    state = json.loads(raw) if raw is not None else {"version": 1, "assignments": {}}
    if state.get("version") != 1 or not isinstance(state.get("assignments"), dict):
        raise ValueError("Invalid assignment ledger; preserve and inspect it")
    if identifier in state["assignments"]:
        return {
            "source": source,
            "found": True,
            "location": "current",
            "assignment": state["assignments"][identifier],
        }
    archived = []
    archive = pathlib.Path("orchestration/archive")
    if any(p.is_symlink() for p in (archive, *archive.parents)):
        raise ValueError("Assignment archive lookup is ambiguous or invalid")
    for path in archive.glob(identifier + "-*.json"):
        match = re.fullmatch(re.escape(identifier) + r"-([a-f0-9]{64})\.json", path.name)
        if not match:
            other = re.fullmatch(
                r"([A-Za-z0-9][A-Za-z0-9_-]{0,63})-([a-f0-9]{64})\.json", path.name
            )
            if other and other[1] != identifier:
                continue  # A different valid assignment may share the requested prefix.
            raise ValueError("Assignment archive lookup is ambiguous or invalid")
        if len(archived) >= 16:
            raise ValueError("Assignment archive lookup is ambiguous or invalid")
        try:
            data = bounded_preparation_bytes(path.as_posix())
            item = strict_package_json(data)
        except (ValueError, OSError) as error:
            raise ValueError("Assignment archive lookup is ambiguous or invalid") from error
        if digest(data) != match[1]:
            raise ValueError("Assignment archive lookup is ambiguous or invalid")
        if (
            not isinstance(item, dict)
            or item.get("id") != identifier
            or item.get("status") != "decided"
        ):
            raise ValueError("Assignment archive lookup is ambiguous or invalid")
        archived.append((path, item, match[1]))
    if len(archived) > 1:
        raise ValueError("Assignment archive lookup is ambiguous or invalid")
    if archived:
        path, item, sha = archived[0]
        return {
            "source": source,
            "found": True,
            "location": "archive",
            "assignment": item,
            "archive_source": {"path": path.as_posix(), "sha256": sha},
            "resubmitted": False,
        }
    return {
        "source": source,
        "found": False,
        "id": identifier,
        "scope": "current_and_verified_archived_assignment_ledger",
        "creation_status": "not_recorded_in_checked_ledger",
        "retry_safe": None,
        "creation_attempt_status": "not_assessed_by_lookup",
        "first_dispatch_rule": "not_a_retry_if_no_previous_creation_attempt",
        "resubmitted": False,
        "notice": "No record in this checked ledger. Creation history and retry safety were not assessed (null means unknown, not unsafe or authorized). An actual first dispatch is not a retry: if no earlier creation attempt exists in actual history, validate and assign normally; do not demand proof that no session could ever exist. A previously submitted uncertain POST still requires receipt/task-hash reconciliation, never blind recreation. Do not repeat this lookup with unchanged source hash.",
    }
