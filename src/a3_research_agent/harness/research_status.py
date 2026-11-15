"""Record resumable research progress and evidenced external blockers."""

import hashlib
import json
from pathlib import Path, PurePosixPath

from . import paths, state_paths
from .context.manager import atomic

EXTERNAL_BLOCKERS = {"authentication", "permission", "unavailable_service", "missing_resource"}


def evidence_exists(workspace: Path, names: object) -> bool:
    if not isinstance(names, list) or not 1 <= len(names) <= 8:
        return False
    root = workspace.resolve()
    try:
        for name in names:
            if not isinstance(name, str) or not name or len(name) > 300:
                return False
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name or "\0" in name:
                return False
            path = (root / name).resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size == 0:
                return False
        return True
    except (OSError, ValueError):
        return False


def validate_blocker(blocker, workspace):
    if not (
        isinstance(blocker, dict)
        and blocker.get("kind") in EXTERNAL_BLOCKERS
        and blocker.get("requires_human") is True
        and all(
            isinstance(blocker.get(key), str) and blocker[key].strip() and len(blocker[key]) <= 1000
            for key in ("reason", "required_action")
        )
        and evidence_exists(workspace, blocker.get("evidence"))
    ):
        raise ValueError(
            "External blockers need a supported kind, required action and existing workspace evidence"
        )
    return {
        key: blocker[key]
        for key in ("kind", "requires_human", "reason", "required_action", "evidence")
    }


def read_outcome(workspace: Path, state_root: Path) -> dict:
    """Only Host-owned state supplies terminal claims; evidence remains research data."""
    try:
        outcome = json.loads(state_paths.path(state_paths.OUTCOME, state_root).read_text())
        if not isinstance(outcome, dict):
            raise TypeError("Research outcome must be an object")
        if outcome.get("status") == "blocked":
            validate_blocker(outcome.get("external_blocker"), workspace)
            return outcome
        return {"status": "continue", "reason": str(outcome.get("reason", "Research continues"))}
    except (OSError, ValueError, TypeError, KeyError) as error:
        return {"status": "continue", "reason": str(error)}


def update(arguments):
    """Apply one explicit outcome tool request without modifying research files."""
    try:
        if arguments.keys() - {"action", "status", "reason", "external_blocker"}:
            raise ValueError("Outcome accepts only action, status, reason and external_blocker")
        status, reason = arguments.get("status"), arguments.get("reason", "")
        if arguments.get("action") != "outcome" or status not in {"continue", "blocked"}:
            raise ValueError("Use outcome status continue or blocked")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("Give a nonempty outcome reason of at most 1000 characters")
        outcome = {"status": status, "reason": reason.strip()}
        if status == "blocked":
            outcome["external_blocker"] = validate_blocker(
                arguments.get("external_blocker"), paths.workspace_root()
            )
        elif "external_blocker" in arguments:
            raise ValueError("Only blocked outcomes accept external_blocker")
        raw = json.dumps(outcome, ensure_ascii=False, sort_keys=True)
        atomic(state_paths.path(state_paths.OUTCOME), raw)
        return {**outcome, "sha256": hashlib.sha256(raw.encode()).hexdigest()}
    except (ValueError, TypeError, KeyError) as error:
        return {"error": "invalid_research_outcome", "hint": str(error)}
