"""Workspaces services used by Host launch."""

import shutil
import subprocess
from pathlib import Path


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def snapshot_checkout(source: Path, destination: Path) -> None:
    subprocess.run(
        ["git", "clone", "--quiet", "--no-local", str(source), str(destination)], check=True
    )
    report_checkout = destination / "reports"
    if report_checkout.is_dir():
        shutil.rmtree(report_checkout)
    files = (
        subprocess.check_output(
            [
                "git",
                "-C",
                str(source),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ]
        )
        .decode()
        .split("\0")
    )
    for name in filter(None, files):
        if Path(name).parts[0] == "reports":
            continue
        src, dst = source / name, destination / name
        if not src.exists() and dst.is_file():
            dst.unlink()
        if src.is_file() and not src.is_symlink():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    git(destination, "config", "user.name", "Research Agent")
    git(destination, "config", "user.email", "agent@localhost")
    # A submodule's host .git indirection must never reach into the live checkout.
    mini = source / "third_party/mini-swe-agent"
    if mini.is_dir():
        target = destination / "third_party/mini-swe-agent"
        target.rmdir()
        snapshot_checkout(mini, target)
    # The selected files and exclusions form this isolated checkout's starting point.
    if git(destination, "status", "--porcelain"):
        git(destination, "add", "--all")
        git(
            destination,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "chore: establish isolated execution baseline",
        )
