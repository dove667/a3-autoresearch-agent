"""Small, read-only readers shared by the local research dashboard."""

import json
import os
from pathlib import Path


def read_json(path, default=None):
    """Read one JSON file, returning a default for missing or malformed data."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def read_text(path, default=""):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return default


def read_json_lines(path, *, max_bytes=None):
    """Read JSON lines, optionally from a bounded tail of the file."""
    lines = tail_lines(path) if max_bytes is None else tail_lines(path, max_bytes=max_bytes)
    rows = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def tail_lines(path, *, max_bytes=2_000_000):
    """Read the last lines of a UTF-8 file without loading a huge history."""
    path = Path(path)
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            start = max(0, size - max_bytes)
            stream.seek(start)
            data = stream.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start and lines:
        # The first line may be a partial UTF-8 sequence boundary.
        lines = lines[1:]
    return lines


def mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def newest_mtime(paths):
    values = (mtime(path) for path in paths)
    return max((value for value in values if value is not None), default=None)


def count_lines(path):
    try:
        with Path(path).open("rb") as stream:
            return sum(1 for _ in stream)
    except OSError:
        return 0
