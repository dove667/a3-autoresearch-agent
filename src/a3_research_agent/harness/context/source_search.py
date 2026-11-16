"""Search current-run text sources through verified, bounded file reads."""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
from contextlib import contextmanager

from a3_research_agent.harness import state_paths

ARCHIVED_OUTPUT = re.compile(r"context-history/tool-output-([0-9a-f]{64})\.json")
TEXT_ARTIFACT = re.compile(r"context-history/artifact-([0-9a-f]{64})\.txt")
CHUNK_CHARACTERS = 65536


@contextmanager
def verified_text(reference, expected_sha256=None):
    """Keep verification and search on one no-follow file descriptor."""
    path = state_paths.path(reference)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as binary:
        before = os.fstat(binary.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("source_must_be_regular_file")
        digest = hashlib.file_digest(binary, "sha256").hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("source_hash_mismatch")
        binary.seek(0)
        stream = io.TextIOWrapper(binary, encoding="utf-8", newline="")
        try:
            yield stream, digest
            after = os.fstat(binary.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("source_changed")
        finally:
            stream.detach()


def find_text(stream, pattern, *, radius=100):
    """Scan all characters while retaining one snippet and distinct matched terms."""
    overlap = max(max(map(len, pattern.pattern.split("|")), default=0), radius) + radius
    tail, consumed, found, terms = "", 0, None, set()
    while chunk := stream.read(CHUNK_CHARACTERS):
        text = tail + chunk
        start = consumed - len(tail)
        for match in pattern.finditer(text):
            terms.add(match.group().casefold())
            if found is None:
                found = {
                    "snippet": text[max(0, match.start() - radius) : match.end() + radius],
                    "offset": max(0, start + match.start() - radius),
                }
            elif (
                found["offset"] + len(found["snippet"]) >= start
                and len(found["snippet"]) < radius * 2
            ):
                # A chunk-boundary match needs its following context from this chunk.
                left = found["offset"] - start
                if left >= 0:
                    found["snippet"] = text[left : left + radius * 2]
        consumed += len(chunk)
        tail = text[-overlap:]
    if found is None:
        return None
    return {**found, "matched_terms": sorted(terms)}


def output_sources(value):
    """Only runtime archive envelopes may expose additional searchable files."""
    if (
        not isinstance(value, dict)
        or value.get("archive_version") != 1
        or value.get("preview_complete") is not False
    ):
        return []
    reference = value.get("path")
    match = ARCHIVED_OUTPUT.fullmatch(reference) if isinstance(reference, str) else None
    if not match:
        raise ValueError("invalid_output_reference")
    sources = [(reference, match[1])]
    artifact = value.get("artifact")
    if artifact is not None:
        if not isinstance(artifact, dict):
            raise ValueError("invalid_artifact_reference")
        if artifact.get("encoding") == "binary":
            return sources
        reference = artifact.get("path")
        match = TEXT_ARTIFACT.fullmatch(reference) if isinstance(reference, str) else None
        if not match or artifact.get("encoding") != "utf-8" or artifact.get("sha256") != match[1]:
            raise ValueError("invalid_artifact_reference")
        sources.append((reference, match[1]))
    return sources
