"""Load an explicit source manifest for a fresh candidate identity."""

import json
from pathlib import Path

from a3_research_agent.workspace.paths import workspace_path


def load_harness_package(
    directory: Path, manifest_name: str = "run.json"
) -> tuple[list[str], dict[str, str]]:
    manifest = json.loads(workspace_path(directory, manifest_name).read_text())
    command, names = manifest["command"], manifest["files"]
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError("Package command must be a nonempty string array")
    if not isinstance(names, list) or not all(isinstance(x, str) for x in names):
        raise ValueError("Package files must be an explicit path list")
    return command, {
        name: workspace_path(directory, name).read_bytes().decode("utf-8") for name in names
    }
