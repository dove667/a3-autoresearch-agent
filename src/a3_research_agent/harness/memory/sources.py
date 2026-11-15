"""Read declared memory sources through bounded, provenance-preserving locators."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
JOB = re.compile(r"(?:run-[A-Za-z0-9_-]+/)?jobs/([0-9a-f]{32})(?:/(.*))?$")
MEMORY = re.compile(r"(?:memory/|memory:|hypothesis:)?([0-9a-f]{12})$")
HISTORICAL_PREFIXES = ("conversation/", "inputs/", "input-revisions/", "plan-snapshots/")


class SourceError(ValueError):
    """A source is unavailable, malformed or outside the declared readable roots."""


def relative_name(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise SourceError("Source must be a relative, declared path")
    return path.as_posix()


def regular_path(root: Path, name: str) -> Path:
    candidate = root / relative_name(name)
    if any(part.is_symlink() for part in (candidate, *candidate.parents)):
        raise SourceError("Symbolic-link sources are not readable")
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise SourceError("Source file is unavailable")
    return candidate


def read_bounded(path: Path, maximum: int = MAX_SOURCE_BYTES) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise SourceError("Source exceeds the bounded text reader; use its job artifact locator")
    return raw


def page(raw: bytes, reference: str, arguments: dict, **metadata) -> dict:
    offset, limit = arguments.get("offset", 0), arguments.get("limit", 4000)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000:
        raise SourceError("Use offset >= 0 and limit from 1 to 8000")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SourceError("Source is binary; use its artifact locator") from error
    end = min(offset + limit, len(text))
    return {
        "ref": reference,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "offset": offset,
        "end": end,
        "total": len(text),
        "eof": end >= len(text),
        "content": text[offset:end],
        **metadata,
    }


class MemorySources:
    """Resolve only references declared by a note and its verified import manifest."""

    def __init__(self, project_root: Path, run_id: str = ""):
        self.root = Path(project_root).resolve()
        self.run_id = run_id
        self._manifests: dict[tuple[str, int, int], dict] = {}

    def imported_source(self, entry: dict) -> dict | None:
        manifest = self.root / "memory/sources.json"
        if not manifest.exists():
            return None
        manifest = regular_path(self.root, "memory/sources.json")
        records = json.loads(read_bounded(manifest, MAX_MANIFEST_BYTES))
        return next((record for record in records if entry["id"] in record.get("ids", [])), None)

    def __call__(self, entry: dict, arguments: dict) -> dict:
        try:
            imported = self.imported_source(entry)
            references = list(entry.get("refs", []))
            if imported:
                references.append("imported-original")
            reference = arguments.get("ref")
            if reference is None:
                return {
                    "id": entry["id"],
                    "sources": [self.locator(ref, imported, entry) for ref in references],
                    "notice": "Original notes preserve what the Agent recorded; experiment evidence is read from the declared job or result reference.",
                }
            if not isinstance(reference, str) or reference not in references:
                raise SourceError("Select an exact ref returned by memory sources")
            if reference == "imported-original":
                return self.original_note(entry, imported, arguments)
            locator = self.locator(reference, imported, entry)
            if locator["kind"] in ("job", "memory"):
                return locator
            if reference.startswith("workspace/"):
                source = regular_path(self.root, reference)
                return page(read_bounded(source), reference, arguments, kind="workspace")
            if reference.startswith("trajectories/"):
                _, trajectory, relative = reference.split("/", 2)
                return self.historical_page(trajectory, relative, reference, arguments)
            if imported:
                trajectory = relative_name(imported["archive"]).split("/")[1]
                return self.historical_page(trajectory, reference, reference, arguments)
            raise SourceError("Source has no resolvable workspace, trajectory or job locator")
        except (SourceError, OSError, ValueError, KeyError, tarfile.TarError) as error:
            return {"error": "memory_source_unavailable", "hint": str(error)}

    def locator(self, reference: str, imported: dict | None, entry: dict) -> dict:
        if reference == "imported-original":
            return {
                "ref": reference,
                "kind": "original_note",
                "path": imported["archive"],
                "note_id": entry["id"],
            }
        job = JOB.fullmatch(reference)
        if job:
            result = {
                "ref": reference,
                "kind": "job",
                "job_id": job[1],
                "tool": "job_status",
                "path": f"/jobs/{job[1]}",
            }
            if job[2]:
                result.update(tool="job_artifact", artifact_path=relative_name(job[2]))
            return result
        memory = MEMORY.fullmatch(reference)
        if memory:
            return {
                "ref": reference,
                "kind": "memory",
                "tool": "memory",
                "action": "read",
                "id": memory[1],
            }
        name = relative_name(reference)
        if name.startswith("workspace/"):
            if not name.startswith(("workspace/scripts/", "workspace/experiments/")):
                raise SourceError("Only research scripts and experiment files are source-readable")
            return {
                "ref": reference,
                "kind": "workspace",
                "available": (self.root / name).is_file(),
            }
        if name.startswith("trajectories/"):
            parts = name.split("/", 2)
            if len(parts) != 3 or not parts[2].startswith(HISTORICAL_PREFIXES):
                raise SourceError(
                    "Only historical conversation, inputs and plan snapshots are readable"
                )
            return {"ref": reference, "kind": "trajectory"}
        return {"ref": reference, "kind": "historical_reference", "resolvable": bool(imported)}

    def original_note(self, entry: dict, imported: dict, arguments: dict) -> dict:
        source = regular_path(self.root, imported["archive"])
        with gzip.open(source, "rb") as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES or hashlib.sha256(raw).hexdigest() != imported["sha256"]:
            raise SourceError("Imported original memory failed its content-hash check")
        for line in raw.splitlines():
            record = json.loads(line)
            if record.get("id") == entry["id"]:
                return page(
                    line,
                    "imported-original",
                    arguments,
                    kind="original_note",
                    path=imported["archive"],
                    archive_sha256=imported["sha256"],
                )
        raise SourceError("Imported original note ID was not found")

    def archive_manifest(self, archive: Path) -> dict:
        stat = archive.stat()
        identity = (str(archive), stat.st_mtime_ns, stat.st_size)
        if identity not in self._manifests:
            with tarfile.open(archive, "r|gz") as stream:
                for member in stream:
                    if member.name == "manifest.json":
                        if not member.isfile() or member.size > MAX_MANIFEST_BYTES:
                            raise SourceError("Invalid history manifest")
                        self._manifests[identity] = json.load(stream.extractfile(member))
                        break
                else:
                    raise SourceError("History manifest is unavailable")
        return self._manifests[identity]["files"]

    def historical_page(
        self, trajectory: str, relative: str, reference: str, arguments: dict
    ) -> dict:
        trajectory = relative_name(trajectory)
        if "/" in trajectory:
            raise SourceError("Invalid trajectory locator")
        relative = relative_name(relative)
        candidates = [relative]
        if not relative.startswith(HISTORICAL_PREFIXES):
            candidates += [f"conversation/{relative}", f"inputs/{relative}"]
        else:
            direct = self.root / "trajectories" / trajectory / relative
            if direct.exists():
                direct = regular_path(self.root, f"trajectories/{trajectory}/{relative}")
                return page(
                    read_bounded(direct),
                    reference,
                    arguments,
                    kind="trajectory",
                    original_path=relative,
                )
        archive = regular_path(self.root, f"trajectories/{trajectory}/model-records.tar.gz")
        manifest = self.archive_manifest(archive)
        selected = next(
            (
                name
                for name in candidates
                if name in manifest and name.startswith(HISTORICAL_PREFIXES)
            ),
            None,
        )
        if selected is None:
            raise SourceError(
                "Referenced file is absent from retained history; use its job reference"
            )
        item = manifest[selected]
        if item["bytes"] > MAX_SOURCE_BYTES or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise SourceError("Invalid or oversized history source")
        wanted = "blobs/" + item["sha256"]
        with tarfile.open(archive, "r|gz") as stream:
            for member in stream:
                if member.name == wanted:
                    if not member.isfile() or member.size != item["bytes"]:
                        raise SourceError("History source size mismatch")
                    raw = stream.extractfile(member).read(MAX_SOURCE_BYTES + 1)
                    if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                        raise SourceError("History source hash mismatch")
                    return page(
                        raw, reference, arguments, kind="trajectory", original_path=selected
                    )
        raise SourceError("History content blob is unavailable")
