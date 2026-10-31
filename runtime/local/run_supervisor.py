"""Keep a Host run alive across builder submissions and descendant sessions."""

import json
import time
from collections.abc import Callable
from pathlib import Path

from runtime.local.candidate_runtime import workspace_path
from runtime.local.candidate_versions import CandidateVersions
from runtime.local.harness_package import load_harness_package

ACTIVE_STATES = {"running", "created", "restarting", "paused"}
EXTERNAL_BLOCKERS = {"authentication", "permission", "unavailable_service", "missing_resource"}


def evidence_exists(workspace: Path, names: object) -> bool:
    if not isinstance(names, list) or not names:
        return False
    try:
        return all(
            isinstance(name, str)
            and (path := workspace_path(workspace, name)).is_file()
            and path.stat().st_size > 0
            for name in names
        )
    except (OSError, ValueError):
        return False


def evaluate_outcome(workspace: Path, required_checks: tuple[str, ...]) -> dict:
    """Validate a declared terminal record; substantive evidence still needs review."""
    try:
        outcome = json.loads(workspace_path(workspace, "run-outcome.json").read_text())
        if not isinstance(outcome, dict):
            raise TypeError("Outcome must be an object")
        if outcome.get("status") == "complete":
            checks = outcome.get("acceptance", {})
            if not isinstance(checks, dict) or not required_checks:
                raise ValueError("Missing required acceptance checks")
            for key in required_checks:
                check = checks.get(key, {})
                if not isinstance(check, dict) or check.get("status") != "passed":
                    raise ValueError(f"Acceptance {key} has not passed")
                if not evidence_exists(workspace, check.get("evidence")):
                    raise ValueError(f"Acceptance {key} needs reachable, nonempty evidence files")
            _, files = load_harness_package(workspace, "continuation.json")
            CandidateVersions.validate_files(files)
            return outcome
        if outcome.get("status") == "blocked":
            blocker = outcome.get("external_blocker", {})
            if (
                isinstance(blocker, dict)
                and blocker.get("kind") in EXTERNAL_BLOCKERS
                and blocker.get("requires_human") is True
                and isinstance(blocker.get("reason"), str)
                and blocker["reason"].strip()
                and isinstance(blocker.get("required_action"), str)
                and blocker["required_action"].strip()
                and evidence_exists(workspace, blocker.get("evidence"))
            ):
                return outcome
            raise ValueError("Temporary capacity, waiting or incomplete work requires continuation")
        return {"status": "continue", "reason": str(outcome.get("reason", "Work is incomplete"))}
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {"status": "continue", "reason": str(error)}


class RunSupervisor:
    def __init__(
        self,
        *,
        run: Path,
        workspace: Path,
        required_checks: tuple[str, ...],
        sessions: Callable[[], list[dict]],
        execute_builder: Callable[[str, Path], None],
        finalize: Callable[[], None] | None = None,
        wait: Callable[[float], None] = time.sleep,
    ):
        self.run_directory = run
        self.workspace = workspace
        self.required_checks = required_checks
        self.sessions = sessions
        self.execute_builder = execute_builder
        self.finalize = finalize
        self.wait = wait
        self.turn = 0

    def record(self, phase: str, outcome: dict, sessions: list[dict]) -> None:
        state = {
            "phase": phase,
            "builder_turn": self.turn,
            "outcome": outcome,
            "sessions": sessions,
            "updated_at": time.time(),
        }
        text = json.dumps(state, ensure_ascii=False, indent=2)
        temporary = self.run_directory / "supervision.tmp"
        temporary.write_text(text)
        temporary.replace(self.run_directory / "supervision.json")
        with (self.run_directory / "supervisor-events.jsonl").open("a") as stream:
            stream.write(json.dumps(state, ensure_ascii=False) + "\n")

    @staticmethod
    def active(sessions: list[dict]) -> list[str]:
        return sorted(
            record["session_id"]
            for record in sessions
            if not record.get("closed") and record.get("status") in ACTIVE_STATES
        )

    def run(self, *, initial_builder: bool = True) -> dict:
        """Start once, preserve workers, and record the result after all workers stop."""
        builder_error = None
        if initial_builder:
            self.turn = 1
            self.record("starting", {}, self.sessions())
            try:
                self.execute_builder("", self.run_directory / "trajectory.json")
            except Exception as error:  # noqa: BLE001 — keep existing workers alive
                builder_error = type(error).__name__
                self.record("builder_error", {"error_type": builder_error}, self.sessions())

        sessions = self.sessions()
        while self.active(sessions):
            self.record("supervising", {}, sessions)
            self.wait(2)
            sessions = self.sessions()

        # Structural checks and snapshot publication happen only after execution ends.
        # Their results never trigger another model call or enter a worker's context.
        outcome = evaluate_outcome(self.workspace, self.required_checks)
        if outcome["status"] == "continue":
            outcome = {**outcome, "status": "incomplete"}
        if builder_error:
            outcome["builder_error"] = builder_error
        if outcome["status"] == "complete" and self.finalize is not None:
            try:
                self.finalize()
            except (OSError, ValueError, RuntimeError) as error:
                outcome = {"status": "archive_error", "error_type": type(error).__name__}
        self.record("stopped", outcome, sessions)
        (self.run_directory / "outcome.json").write_text(
            json.dumps(outcome, ensure_ascii=False, indent=2)
        )
        return outcome
