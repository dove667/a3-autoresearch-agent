"""Directories services used by Host launch."""

import os
from datetime import datetime
from pathlib import Path


def create_run_directory(root: Path) -> Path:
    """Reserve a directory named with the Host's local start time."""
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("run-%Y%m%d-%H%M%S-%f")
    sequence = 1
    while True:
        name = timestamp if sequence == 1 else f"{timestamp}-{sequence}"
        run = root / name
        try:
            run.mkdir(mode=0o700)
        except FileExistsError:
            sequence += 1
        else:
            return run


def local_storage_bytes(run: Path) -> int:
    """Count this run's files without following links into source or other runs."""
    total = 0
    for directory, names, files in os.walk(run, followlinks=False):
        names[:] = [name for name in names if not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            try:
                if not path.is_symlink() and path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass
    return total
