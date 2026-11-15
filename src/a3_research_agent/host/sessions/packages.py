"""Read the current published-Harness seed and continuation declarations."""

import json
import re
from pathlib import Path, PurePosixPath

from a3_research_agent.host.published_harness import COMMAND
from a3_research_agent.host.runs.checkpoints import require_tool_results
from a3_research_agent.path_safety import workspace_path


def declared_files(manifest: dict) -> list[str]:
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("version")) is not int
        or manifest["version"] != 1
        or manifest.get("command") != COMMAND
    ):
        raise ValueError("Expected a version-1 published Harness declaration")
    names = manifest.get("files")
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise ValueError("Published files must be an explicit path list")
    if len(set(names)) != len(names):
        raise ValueError("Published files must be a unique explicit path list")
    for name in names:
        path = PurePosixPath(name)
        if (
            not name
            or not path.parts
            or name != path.as_posix()
            or path.is_absolute()
            or "\\" in name
            or "\0" in name
            or any(part in {".", ".."} for part in path.parts)
        ):
            raise ValueError("Published files need canonical relative paths")
    return names


def load_published_manifest(directory: Path) -> tuple[str, list[str], dict[str, str]]:
    manifest = json.loads(workspace_path(directory, "run.json").read_text())
    if not isinstance(manifest, dict) or set(manifest) != {
        "version",
        "command",
        "files",
        "harness_commit",
    }:
        raise ValueError("Published Harness seed has unsupported fields")
    names = declared_files(manifest)
    commit = manifest.get("harness_commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Published Harness seed requires its full commit SHA")
    checkpoint = workspace_path(directory, "checkpoint.json").read_text()
    if names != ["checkpoint.json"] or json.loads(checkpoint) != []:
        raise ValueError("Published Harness seed must contain only an empty checkpoint")
    return commit, COMMAND.copy(), {"checkpoint.json": "[]"}


def read_continuation(workspace: Path) -> dict:
    manifest = json.loads(workspace_path(workspace, "continuation.json").read_text())
    names = declared_files(manifest)
    if "checkpoint.json" not in names:
        raise ValueError("Continuation must declare its native checkpoint")
    for name in names:
        if not workspace_path(workspace, name).is_file():
            raise ValueError(f"Continuation file is unavailable: {name}")
    messages = json.loads(workspace_path(workspace, "checkpoint.json").read_text())
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        raise ValueError("Published Harness checkpoint must be a message list")
    require_tool_results(messages)
    return manifest
