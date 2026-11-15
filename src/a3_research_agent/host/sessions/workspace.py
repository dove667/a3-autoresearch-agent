"""Current native session workspace paths and bounded file operations."""

import base64
import hashlib
import os
import stat
from pathlib import Path, PurePosixPath

from a3_research_agent.host.published_harness import research_workspace
from a3_research_agent.path_safety import workspace_path

ACTIVE_STATES = {"created", "running"}
MAX_ACTIVE_SESSIONS = 1
MAX_INPUT_FILES = 128
MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
DEFAULT_ARTIFACT_PAGE_BYTES = 1024 * 1024


class SessionCapacityError(RuntimeError):
    """The current run already has an active research process."""


class ArtifactTooLargeError(ValueError):
    def __init__(self, size_bytes: int):
        super().__init__("Artifact exceeds the whole-file response limit")
        self.size_bytes = size_bytes


def require_native_record(record: dict) -> None:
    if (
        not isinstance(record, dict)
        or type(record.get("version")) is not int
        or record["version"] != 1
        or record.get("backend") != "process"
    ):
        raise ValueError("Unsupported local session record schema")


def exit_archived(record: dict) -> bool:
    require_native_record(record)
    return record.get("exit_archived") is True and record.get("terminal_state", {}).get(
        "status"
    ) in {"exited", "dead"}


def session_workspace(root: Path, session: str) -> Path:
    if root.name != "sessions":
        raise ValueError("Session metadata must use the current run/sessions layout")
    directory = workspace_path(root, session)
    link = directory / "workspace"
    expected = research_workspace(root.parent, session)
    if not link.is_symlink() or link.resolve(strict=True) != expected.resolve(strict=True):
        raise ValueError(
            "Session workspace must reference its current Host-managed research directory"
        )
    return expected


def write_workspace_files(root: Path, files: dict[str, str]) -> None:
    if (
        len(files) > MAX_INPUT_FILES
        or sum(len(value.encode()) for value in files.values()) > MAX_INPUT_BYTES
    ):
        raise ValueError("Session input exceeds 128 files or 20 MiB")
    targets = [(workspace_path(root, name), content) for name, content in files.items()]
    for path, content in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def write_durable(path: Path, content: str) -> None:
    """Commit session evidence before publishing its next state."""
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_workspace_files(root: Path, references: list[dict]) -> tuple[dict, list]:
    """Read bounded, hash-pinned UTF-8 source under the research directory."""
    selected, manifest, total = {}, [], 0
    if not 1 <= len(references) <= 16:
        raise ValueError("Select 1..16 workspace research files")
    for reference in references:
        name, expected = reference["path"], reference["sha256"]
        path = PurePosixPath(name)
        if (
            name != path.as_posix()
            or "\\" in name
            or path.is_absolute()
            or len(path.parts) < 2
            or path.parts[0] != "research"
            or any(part in {"..", "."} or part.startswith(".") for part in path.parts)
            or name in selected
        ):
            raise ValueError("Workspace attachments need unique canonical research paths")
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        leaf = None
        try:
            for part in path.parts[:-1]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
            leaf = os.open(
                path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            info = os.fstat(leaf)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise ValueError("Workspace attachment must be a regular file of at most 1 MiB")
            with os.fdopen(leaf, "rb") as stream:
                leaf = None
                raw = stream.read(1024 * 1024 + 1)
            actual = hashlib.sha256(raw).hexdigest()
            if len(raw) > 1024 * 1024 or actual != expected:
                raise ValueError("Workspace attachment size or SHA256 changed")
            total += len(raw)
            if total > 8 * 1024 * 1024:
                raise ValueError("Workspace attachments exceed 8 MiB")
            selected[name] = raw.decode("utf-8")
            manifest.append({"path": name, "sha256": actual, "bytes": len(raw)})
        except (OSError, UnicodeError) as error:
            raise ValueError("Workspace attachment unavailable, unsafe or not UTF8") from error
        finally:
            if leaf is not None:
                os.close(leaf)
            os.close(directory)
    return selected, manifest


def read_artifact(root: Path, name: str, offset: int = 0, limit: int | None = None) -> dict:
    path = workspace_path(root, name)
    if not path.is_file():
        raise ValueError("Artifact must be a workspace file")
    paged = limit is not None or offset != 0
    read_limit = (
        limit
        if limit is not None
        else (DEFAULT_ARTIFACT_PAGE_BYTES if paged else MAX_ARTIFACT_BYTES)
    )
    if offset < 0 or not 1 <= read_limit <= MAX_ARTIFACT_BYTES:
        raise ValueError("Invalid artifact page bounds")
    size = path.stat().st_size
    if not paged and size > MAX_ARTIFACT_BYTES:
        raise ArtifactTooLargeError(size)
    if offset > size:
        raise ValueError("Artifact offset exceeds file size")
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(min(read_limit, size - offset))
    result = {"path": name, "encoding": "base64", "data": base64.b64encode(data).decode()}
    if paged:
        result.update(
            offset=offset,
            next_offset=offset + len(data),
            size_bytes=size,
            eof=offset + len(data) >= size,
        )
    return result
