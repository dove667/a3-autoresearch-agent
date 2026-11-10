"""Resolve maintainer-published source and a separate writable research workspace."""

import io
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

from a3_research_agent.host.inputs.versions import write_atomic
from a3_research_agent.workspace.paths import workspace_path

COMMAND = ["python", "-P", "-m", "a3_research_agent.harness.agent", "--resume"]
MARKER = ".host/research-workspace.json"


def project_for_run(run: Path) -> Path:
    if run.parent.name != "rsi" or run.parent.parent.name != "artifacts":
        raise ValueError("Published Harness runs must live under artifacts/rsi")
    return run.parents[2]


def run_commit(run: Path) -> str | None:
    marker = workspace_path(run, MARKER)
    if not marker.is_file():
        return None
    commit = json.loads(marker.read_text())["commit"]
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Invalid pinned Harness commit")
    return commit


def pin_run(run: Path, commit: str) -> None:
    existing = run_commit(run)
    if existing is not None and existing != commit:
        raise ValueError("Cannot change a run's pinned Harness")
    write_atomic(workspace_path(run, MARKER), json.dumps({"commit": commit}).encode())


def research_workspace(run: Path, session: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", session):
        raise ValueError("Invalid research workspace identity")
    return workspace_path(project_for_run(run), f"workspace/{run.name}/{session}")


def pinned_resume(workspace: Path) -> tuple[list[str], dict[str, str]]:
    return COMMAND.copy(), {
        "checkpoint.json": workspace_path(workspace, "checkpoint.json").read_text()
    }


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
    source = workspace_path(root, f"artifacts/published-harness/v2/{commit}")
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
