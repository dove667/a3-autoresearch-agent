"""Validate workspace-relative paths without container dependencies."""

from pathlib import Path, PurePosixPath


def workspace_path(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if (
        not name
        or relative.is_absolute()
        or ".." in relative.parts
        or relative == PurePosixPath(".")
    ):
        raise ValueError("Expected a relative workspace file")
    target = root
    for part in relative.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError("Symbolic link paths are not allowed")
    return target
