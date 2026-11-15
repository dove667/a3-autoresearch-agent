"""Start continuous research from the published prompts and retained knowledge."""

import hashlib
import json
from pathlib import Path

from a3_research_agent.harness import state_paths
from a3_research_agent.host.runs.continuation import prepare_fresh_research
from a3_research_agent.path_safety import workspace_path


def input_hashes(inputs: Path) -> dict[str, str]:
    manifest = json.loads(workspace_path(inputs, "input-manifest.json").read_text())
    if manifest.get("status") != "active" or not isinstance(manifest.get("files"), dict):
        raise ValueError("Research inputs require an active versioned manifest")
    hashes = {}
    for name, record in manifest["files"].items():
        digest = hashlib.sha256(workspace_path(inputs, name).read_bytes()).hexdigest()
        if digest != record["sha256"]:
            raise ValueError("Research input differs from its active manifest")
        hashes[name] = digest
    return hashes


def prepare_research_start(start):
    """Create an empty model context and retain the selected research knowledge."""
    fresh, context = prepare_fresh_research(start)
    fresh.files.update(
        {
            state_paths.OUTCOME: json.dumps({"status": "continue", "reason": "Research continues"}),
        }
    )
    context["source_run"] = str(start.previous_run.resolve()) if start.previous_run else None
    return fresh, context
