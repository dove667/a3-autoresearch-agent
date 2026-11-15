"""Observe research sessions and archive their result after execution stops."""

import json
import time
from collections.abc import Callable
from pathlib import Path

from a3_research_agent.harness.research_status import read_outcome
from a3_research_agent.host.published_harness import conversation_root

ACTIVE_STATES = {"running", "created", "restarting", "paused"}


def evaluate_outcome(workspace: Path, state_root: Path) -> dict:
    """Read the saved outcome through the shared research status contract."""
    return read_outcome(workspace, state_root)


class RunSupervisor:
    def __init__(
        self,
        *,
        run: Path,
        workspace: Path,
        sessions: Callable[[], list[dict]],
        retain: Callable[[], None] | None = None,
        wait: Callable[[float], None] = time.sleep,
        result_workspace: Callable[[], Path] | None = None,
        input_manifest: Callable[[], dict] | None = None,
        on_finishing: Callable[[], None] | None = None,
        log_directory: Path | None = None,
    ):
        self.run_directory = run
        self.workspace = workspace
        self.sessions = sessions
        self.retain = retain
        self.wait = wait
        self.result_workspace = result_workspace
        self.input_manifest = input_manifest
        self.on_finishing = on_finishing
        self.last_event = None
        self.log_directory = log_directory if log_directory is not None else run / "logs"

    def record(self, phase: str, outcome: dict, sessions: list[dict]) -> None:
        state = {
            "phase": phase,
            "outcome": outcome,
            "sessions": sessions,
            "updated_at": time.time(),
        }
        if self.input_manifest is not None:
            state["input_manifest"] = outcome.get("input_manifest") or self.input_manifest()
        text = json.dumps(state, ensure_ascii=False, indent=2)
        temporary = self.run_directory / "supervision.tmp"
        temporary.write_text(text)
        temporary.replace(self.run_directory / "supervision.json")
        event = json.dumps(
            {key: value for key, value in state.items() if key != "updated_at"}, sort_keys=True
        )
        if event != self.last_event:
            self.log_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (self.log_directory / "supervisor-events.jsonl").open("a") as stream:
                stream.write(json.dumps(state, ensure_ascii=False) + "\n")
            self.last_event = event

    @staticmethod
    def active(sessions: list[dict]) -> list[str]:
        return sorted(
            record["session_id"]
            for record in sessions
            if not record.get("closed") and record.get("status") in ACTIVE_STATES
        )

    def run(self) -> dict:
        """Observe the process until it stops, then preserve its progress."""

        sessions = self.sessions()
        while self.active(sessions):
            self.record("supervising", {}, sessions)
            self.wait(15)
            sessions = self.sessions()

        if self.on_finishing is not None:
            self.on_finishing()
        if self.result_workspace is not None:
            self.workspace = self.result_workspace()
        manifest = self.input_manifest() if self.input_manifest is not None else None
        outcome = evaluate_outcome(self.workspace, conversation_root(self.run_directory))
        if manifest is not None:
            outcome["input_manifest"] = manifest
        if outcome["status"] == "continue":
            outcome = {**outcome, "status": "incomplete"}
        if self.retain is not None:
            try:
                self.retain()
            except (OSError, ValueError, RuntimeError) as error:
                outcome["retention_error"] = type(error).__name__
        self.record("stopped", outcome, sessions)
        (self.run_directory / "outcome.json").write_text(
            json.dumps(outcome, ensure_ascii=False, indent=2)
        )
        return outcome
