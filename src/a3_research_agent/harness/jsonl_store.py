"""Append and read durable JSONL records used by history and research notes."""

import json
import os
import pathlib
import threading

LOCK = threading.RLock()


def append_record(path, payload):
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with LOCK:
        needs_newline = False
        if target.exists() and target.stat().st_size:
            with target.open("rb") as previous:
                previous.seek(-1, os.SEEK_END)
                needs_newline = previous.read(1) != b"\n"
        with target.open("a", encoding="utf-8") as stream:
            if needs_newline:
                stream.write("\n")
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return payload


def load_records(path):
    target = pathlib.Path(path)
    if not target.is_file():
        return []
    rows = []
    with target.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                # A truncated tail must not destroy earlier evidence.
                rows.append({"type": "corrupt_line", "line": number})
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows
