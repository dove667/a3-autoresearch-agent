"""Resolve migrated implementation paths for source-inspection tests.

Actual process delivery tests use build_harness_payload instead of this resolver.
An explicit HARNESS_SOURCE continues to select an external flat historical overlay.
"""

from pathlib import Path

SOURCE_NAMES = {
    "agent.py": "agent.py",
    "context_manager.py": "context/manager.py",
    "compaction.py": "context/compaction.py",
    "spill.py": "context/spill.py",
    "recall.py": "memory/recall.py",
    "memory_state.py": "memory/state.py",
    "analyzer_client.py": "analysis/client.py",
    "work_assignments.py": "orchestration/assignments.py",
}


class HarnessSources:
    def __init__(self, legacy):
        self.legacy = Path(legacy)
        self.project = Path(__file__).resolve().parents[2]
        self.migrated = self.legacy == self.project / "runtime/harness_recovery"

    def __truediv__(self, name):
        if not self.migrated:
            return self.legacy / name
        name = str(name)
        if name in SOURCE_NAMES:
            return self.project / "src/a3_research_agent/harness" / SOURCE_NAMES[name]
        if name.startswith("a3_research_agent/"):
            return self.project / "src" / name
        return self.legacy / name

    def __str__(self):
        return str(self.legacy)

    def __fspath__(self):
        return str(self.legacy)
