"""Record Agent-owned trial decisions and a single line of main identities."""

import hashlib
import json
from pathlib import Path


class EvolutionState:
    def __init__(self, path: Path, main_owner: str):
        self.path = path
        self.main_owner = main_owner
        self.trials: dict[str, dict] = {}
        self.transitions: list[dict] = []

    def snapshot(self) -> dict:
        return {
            "main_owner": self.main_owner,
            "trials": self.trials,
            "transitions": self.transitions,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2))
        temporary.replace(self.path)

    def require_main(self, actor: str) -> None:
        if actor != self.main_owner:
            raise PermissionError("Only the current main Agent can decide")

    def pending(self) -> bool:
        return any(t["status"] == "testing" for t in self.trials.values())

    def register(self, session: str, baseline: str, candidate: str, checklist: str) -> None:
        self.trials[session] = {
            "proposer": self.main_owner,
            "baseline_version": baseline,
            "candidate_version": candidate,
            "checklist": checklist,
            "checklist_sha256": hashlib.sha256(checklist.encode()).hexdigest(),
            "status": "testing",
        }
        self.save()

    def decide(
        self, actor: str, session: str, retain: bool, reason: str, evidence: list[str]
    ) -> dict:
        self.require_main(actor)
        trial = self.trials[session]
        trial.update(status="retained" if retain else "rejected", reason=reason, evidence=evidence)
        if retain:
            self.transitions.append(
                {"from": actor, "to": session, "version_id": trial["candidate_version"]}
            )
            self.main_owner = session
        self.save()
        return self.snapshot()
