"""Resolve infrastructure files independently from the separate A3 simulation project."""

from pathlib import Path


def checkout_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").is_file() and (candidate / "agent-inputs").is_dir():
        return candidate
    return None


def resolve_project_root(explicit: Path | None = None) -> Path:
    root = explicit if explicit is not None else checkout_root()
    if root is None or not root.is_dir():
        raise ValueError("An infrastructure project root is required; pass --project-root")
    return root.resolve()
