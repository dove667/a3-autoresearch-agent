"""First-level compression: spill oversized tool results to disk.

A tool result larger than the inline budget is written under ``.autorun-spill/``
and the model only receives a bounded head plus the path. This stage never calls
a model, so it is the cheapest way to buy context room before summarising.

The head is explicitly marked as incomplete: the model must read the stored path
with ``read`` (offset/limit pages) instead of assuming the rest matches its guess.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib

DEFAULT_SPILL_BYTES = 50_000
HEAD_CHARS = 2000
SPILL_DIR = ".autorun-spill"
NOTICE = (
    "Tool result exceeded the inline budget and is stored losslessly on disk. "
    "This is a bounded head, not the complete result. Read the path with `read` "
    "using small offset/limit pages when you need the rest; never assume the "
    "missing bytes confirm your hypothesis."
)


def inline_limit(max_bytes=None):
    """Resolve the inline byte budget; <=0 disables spilling."""
    if max_bytes is not None:
        return int(max_bytes)
    raw = os.environ.get("AUTORUN_SPILL_BYTES", "")
    if raw.strip():
        try:
            return int(raw)
        except ValueError:
            return DEFAULT_SPILL_BYTES
    return DEFAULT_SPILL_BYTES


def byte_length(text):
    return len(text.encode("utf-8"))


def maybe_spill(text, *, max_bytes=None, directory=SPILL_DIR, label="result"):
    """Return ``text`` unchanged, or a bounded reference payload when too large."""
    if not isinstance(text, str):
        return text
    limit = inline_limit(max_bytes)
    if limit <= 0 or byte_length(text) <= limit:
        return text
    root = pathlib.Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    path = root / f"{label}-{digest[:16]}.txt"
    if not path.exists():
        path.write_text(text, encoding="utf-8")
    payload = {
        "spilled": True,
        "path": str(path),
        "bytes": byte_length(text),
        "inline_limit_bytes": limit,
        "head": text[:HEAD_CHARS],
        "head_complete": False,
        "notice": NOTICE,
    }
    return json.dumps(payload, ensure_ascii=False)
