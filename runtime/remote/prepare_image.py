"""Prepare a container build context from an existing project environment."""

import argparse
import shutil
import subprocess
from pathlib import Path


def copy_public_references(project: Path, destination: Path) -> None:
    """Keep public usage instructions and their entry scripts with the A3 baseline."""
    required = (
        "README.md",
        "docs/workflow_guide.md",
        "docs/contracts.md",
        "examples/benchmark_cookie_batch.py",
    )
    missing = [name for name in required if not (project / name).is_file()]
    if missing:
        raise ValueError("Missing A3 reference files: " + ", ".join(missing))
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project / "README.md", destination / "README.md")
    for name in ("docs", "examples"):
        shutil.copytree(
            project / name,
            destination / name,
            ignore=shutil.ignore_patterns(".git", ".env", ".env.*", "__pycache__", "*.pyc"),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    project = args.project.resolve()
    context = args.context.resolve()
    context.mkdir(parents=True, exist_ok=False)
    shutil.copy2(Path(__file__).with_name("Dockerfile"), context / "Dockerfile")
    baseline = context / "a3"
    copy_public_references(project, baseline)
    for name in (
        "src",
        "assets",
        "configs",
        ".venv",
        "pyproject.toml",
        "setup.py",
        "LICENSE",
    ):
        source = project / name
        if source.exists():
            subprocess.run(["cp", "-al", str(source), str(baseline / name)], check=True)
    (baseline / "models").mkdir()
    for model in (project / "models").glob("*.xml"):
        shutil.copy2(model, baseline / "models" / model.name)
    (baseline / ".runtime").mkdir()
    subprocess.run(
        ["cp", "-al", str(project / ".runtime/ffmpeg"), str(baseline / ".runtime/ffmpeg")],
        check=True,
    )
    interpreter = (project / ".venv/bin/python").resolve()
    shutil.copytree(interpreter.parent.parent, context / "python", symlinks=True)
    subprocess.run(["docker", "build", "-t", args.image, str(context)], check=True)


if __name__ == "__main__":
    main()
