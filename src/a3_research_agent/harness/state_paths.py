"""Resolve Agent machine state and logical references in its Host-owned store."""

import os
from pathlib import Path, PurePosixPath

LAYOUT_VERSION = 2
CHECKPOINT = "checkpoint.json"
JOURNAL = "pending-calls.json"
HISTORY = "history.jsonl"
TRAJECTORY = "trajectory.jsonl"
OPERATIONS = "operational-state.json"
COMPACTION_RESULT = "compaction-result.json"
COMPACTION_EPOCH = "compaction-epoch.json"
LOOP_FAILURE = "loop-failure.json"
CONTEXT_FAILURE = "context-failure.json"
SUMMARY_BREAKER = "summary-breaker.json"
LEDGER = "research-ledger.jsonl"
ARCHIVES = "context-history"
CONTINUATION = "continuation.json"
RESUME = "resume.json"
OUTCOME = "run-outcome.json"


def state_root(root=None):
    value = root if root is not None else os.environ.get("A3_STATE_ROOT")
    if not value or not Path(value).is_absolute():
        raise RuntimeError("Host must configure an absolute Agent state root")
    directory = Path(value)
    if directory.is_symlink():
        raise ValueError("Agent state root cannot be a symlink")
    return directory.resolve()


def path(name, root=None):
    """Resolve a logical store name without following symlink components."""
    relative = PurePosixPath(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts or "\\" in str(name):
        raise ValueError("Expected a relative Agent state path")
    target = state_root(root)
    for part in relative.parts:
        target /= part
        if target.is_symlink():
            raise ValueError("Agent state paths cannot be symlinks")
    return target
