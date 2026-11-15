"""Resolve maintainer-published source and a separate writable research workspace."""

import io
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

from a3_research_agent.harness import state_paths
from a3_research_agent.host.inputs.versions import write_atomic
from a3_research_agent.path_safety import workspace_path

COMMAND = ["python", "-P", "-m", "a3_research_agent.harness.agent", "--resume"]
MARKER = ".host/research-workspace.json"


def project_for_run(run: Path) -> Path:
    if run.parent.name != "trajectories":
        raise ValueError("Published Harness runs must live under trajectories")
    return run.parents[1]


def run_metadata(run: Path) -> dict:
    marker = workspace_path(run, MARKER)
    metadata = json.loads(marker.read_text()) if marker.is_file() else {}
    if (
        not isinstance(metadata, dict)
        or set(metadata) - {"commit", "run_id"}
        or ("run_id" in metadata and not isinstance(metadata["run_id"], str))
    ):
        raise ValueError("Invalid research workspace metadata")
    return metadata


def run_identity(run: Path) -> str:
    """Keep resource ownership stable when an operator relocates a trajectory."""
    identity = run_metadata(run).get("run_id", run.name)
    if not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identity):
        raise ValueError("Invalid research run identity")
    return identity


def run_commit(run: Path) -> str | None:
    marker = workspace_path(run, MARKER)
    if not marker.is_file():
        return None
    commit = run_metadata(run).get("commit")
    if commit is None:
        return None
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Invalid pinned Harness commit")
    return commit


def pin_run(run: Path, commit: str) -> None:
    existing = run_commit(run)
    if existing is not None and existing != commit:
        raise ValueError("Cannot change a run's pinned Harness")
    metadata = run_metadata(run)
    metadata.update(commit=commit, run_id=run_identity(run))
    metadata.pop("workspace_root", None)
    write_atomic(workspace_path(run, MARKER), json.dumps(metadata).encode())


def replace_run_pin(run: Path, previous: str, commit: str) -> None:
    """Advance a published pin only through the offline release transaction."""
    if run_commit(run) != previous:
        raise ValueError("Published Harness pin changed during release")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Invalid published Harness release commit")
    metadata = {**run_metadata(run), "commit": commit}
    metadata.pop("workspace_root", None)
    write_atomic(workspace_path(run, MARKER), json.dumps(metadata).encode())


def research_workspace(run: Path, session: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", session):
        raise ValueError("Invalid research workspace identity")
    run_metadata(run)
    return workspace_path(project_for_run(run), "workspace")


def pinned_resume(workspace: Path) -> tuple[list[str], dict[str, str]]:
    return COMMAND.copy(), {
        state_paths.CHECKPOINT: workspace_path(workspace, state_paths.CHECKPOINT).read_text()
    }


def native_source(root: Path, commit: str) -> Path:
    """Require the published path contract before any native run is created."""
    from a3_research_agent.host.sessions.processes import ProcessRuntime

    source = published_source(root, commit)
    ProcessRuntime.require_native_source(source)
    return source


def published_source(root: Path, commit: str) -> Path:
    """Cache a single verified source tree per commit, without Git metadata or secrets."""
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Select the full published Git commit SHA")
    git = ["git", "-C", str(root)]
    if subprocess.run(
        [*git, "merge-base", "--is-ancestor", commit, "refs/remotes/origin/main"],
        capture_output=True,
        check=False,
        timeout=15,
    ).returncode:
        raise ValueError("Harness commit must be reachable from origin/main")
    archive = subprocess.check_output(
        [
            *git,
            "archive",
            commit,
            "src/a3_research_agent/harness",
            "src/a3_research_agent/__init__.py",
            "src/a3_research_agent/token_count.py",
        ],
        timeout=30,
    )
    # Earlier cache entries omitted the shared tokenizer module.
    source = workspace_path(root, f".runtime/published-harness/v2/{commit}")
    source.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        files = {}
        for item in tree:
            if item.isdir():
                continue
            if not item.isfile() or not item.name.startswith("src/a3_research_agent/"):
                raise ValueError("Published Harness contains an unsupported source entry")
            name = item.name.removeprefix("src/")
            if not name.endswith(".py") or any(part.startswith(".") for part in Path(name).parts):
                raise ValueError("Published Harness must contain only public Python source")
            files[name] = tree.extractfile(item).read()
    if "a3_research_agent/harness/agent.py" not in files:
        raise ValueError("Published commit has no runnable Harness")
    if not source.exists():
        with tempfile.TemporaryDirectory(dir=source.parent) as directory:
            staging = Path(directory) / "source"
            for name, content in files.items():
                path = workspace_path(staging, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            try:
                staging.rename(source)
            except OSError:
                if not source.is_dir():
                    raise
    actual = {
        path.relative_to(source).as_posix()
        for path in source.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual != files.keys() or any(
        workspace_path(source, name).read_bytes() != content for name, content in files.items()
    ):
        raise ValueError("Published Harness cache differs from the pinned commit")
    return source
