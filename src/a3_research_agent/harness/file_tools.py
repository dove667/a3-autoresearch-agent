"""Read and modify workspace files with bounded pages and explicit revisions."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from . import operational_state, paths, state_paths, workspace_tools
from .context import manager
from .tool_contracts import ToolInputError, arguments, definition, fail, integer

MAX_FILE_BYTES = 1024 * 1024
STRING = {"type": "string"}
READ_FIELDS = {
    "path": STRING,
    "offset": {"type": "integer", "minimum": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 8000},
    "fields": {"type": "array", "items": STRING, "minItems": 1, "maxItems": 32},
}
LIST_FIELDS = {
    "path": STRING,
    "offset": {"type": "integer", "minimum": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
}
WRITE_FIELDS = {
    "path": STRING,
    "content": STRING,
    "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
}
TOOLS = [
    definition(
        "list",
        "List an accessible directory as name/path/type entries. Defaults path=workspace, limit=50; continue with next_offset.",
        LIST_FIELDS,
    ),
    definition(
        "read",
        "Read a bounded character page, default limit=4000. Returns whole-file SHA256 and next_offset. Use fields to select top-level JSON keys. Previously delivered pages can return a context reference.",
        READ_FIELDS,
        ["path"],
    ),
    definition(
        "write",
        "Create a UTF-8 workspace file. Replacing an existing file requires its current expected_sha256 from read. Writes are atomic, at most 1 MiB; use edit for a small change.",
        WRITE_FIELDS,
        ["path", "content"],
    ),
    *workspace_tools.TOOLS,
]
NAMES = frozenset(tool["name"] for tool in TOOLS)


def select_fields(raw, fields, path):
    if (
        not isinstance(fields, list)
        or not 1 <= len(fields) <= 32
        or any(not isinstance(key, str) or len(key) > 200 for key in fields)
    ):
        fail("invalid_fields", "Supply 1–32 top-level JSON keys, each at most 200 characters.")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        fail(
            "source_not_json", "Read a character page without fields; this file is not valid JSON."
        )
    if not isinstance(value, dict):
        fail("json_root_not_object", "Use character pages; fields selects keys from a JSON object.")
    result = {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "fields": {key: value[key] for key in fields if key in value},
        "missing_fields": [key for key in fields if key not in value],
    }
    if manager.size(result) > 10000:
        fail(
            "field_selection_exceeds_page_budget",
            "Select fewer keys or use bounded character pages.",
        )
    return result


def write(path, content, expected):
    if not isinstance(content, str) or len(content.encode()) > MAX_FILE_BYTES:
        fail(
            "invalid_content",
            "Write UTF-8 text of at most 1 MiB; use remote exports for binary or large assets.",
        )
    if path.is_dir():
        fail("path_is_directory", "Choose a file path inside workspace.")
    existed = path.exists()
    if existed and path.stat().st_size > MAX_FILE_BYTES:
        fail("file_too_large", "Use remote assets for files larger than 1 MiB.")
    previous = path.read_bytes() if existed else None
    digest = hashlib.sha256(previous).hexdigest() if previous is not None else None
    if expected != digest:
        return {
            "error": "revision_conflict",
            "path": str(path),
            "current_sha256": digest,
            "hint": "Read the existing file and provide its current expected_sha256 before replacing it.",
        }
    raw = content.encode()
    if previous == raw:
        return {"path": str(path), "status": "unchanged", "sha256": digest, "bytes": len(raw)}
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if existed:
            if not path.is_file() or path.read_bytes() != previous:
                fail("revision_conflict", "The file changed during writing; read it again.")
            os.chmod(staged, path.stat().st_mode & 0o777)
            staged.replace(path)
        else:
            try:
                os.link(staged, path)
            except FileExistsError:
                fail(
                    "revision_conflict",
                    "Another operation created this file; read it before replacing it.",
                )
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        staged.unlink(missing_ok=True)
    return {
        "path": str(path),
        "status": "replaced" if existed else "created",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }


def workspace_alias(value, root):
    """Accept the common ``workspace/...`` prefix only when it is not a real path.

    Weak models often treat the workspace root as a directory named ``workspace``.
    The canonical tool paths are relative to that root, so map the alias only when
    the literal path is absent; a real ``workspace/`` subdirectory keeps priority.
    """
    if not isinstance(value, str):
        return value
    if value == "workspace":
        alias = "."
    elif value.startswith("workspace/"):
        alias = value.removeprefix("workspace/")
    else:
        return value
    if (root / value).exists():
        return value
    if alias == "." or (root / alias).exists():
        return alias
    return value


def dispatch(name, args, *, workspace=None, readonly_roots=None):
    root = Path(workspace).resolve() if workspace is not None else paths.workspace_root()
    readonly = (
        list(readonly_roots)
        if readonly_roots is not None
        else [paths.reference_root(), paths.input_root(), state_paths.state_root()]
    )
    readonly = [Path(value).resolve() for value in readonly]
    try:
        if name in workspace_tools.NAMES:
            args = {**args, "path": workspace_alias(args.get("path", "."), root)}
            return workspace_tools.dispatch(name, args, workspace=root, readonly_roots=readonly)
        fields = {"read": READ_FIELDS, "write": WRITE_FIELDS, "list": LIST_FIELDS}.get(name)
        if fields is None:
            fail("unknown_file_tool", "Use list, read, write, search_files or edit.")
        arguments(args, fields)
        args = {**args, "path": workspace_alias(args.get("path", "."), root)}
        path = workspace_tools.accessible_path(
            args.get("path", "."), root, readonly, writable=name == "write"
        )
        if name == "write":
            return write(path, args.get("content"), args.get("expected_sha256"))
        offset = integer(args.get("offset", 0), "offset", 0, 2**63 - 1)
        if name == "list":
            limit = integer(args.get("limit", 50), "limit", 1, 200)
            if not path.is_dir():
                fail(
                    "path_is_file" if path.is_file() else "file_not_found",
                    "List requires an existing directory; read a file instead.",
                )
            children = sorted(path.iterdir(), key=lambda child: child.name)
            return {
                "path": str(path),
                "entries": [
                    {
                        "name": child.name,
                        "path": str(child),
                        "type": "symlink"
                        if child.is_symlink()
                        else "directory"
                        if child.is_dir()
                        else "file",
                    }
                    for child in children[offset : offset + limit]
                ],
                "total": len(children),
                "offset": offset,
                "next_offset": offset + limit if offset + limit < len(children) else None,
            }
        if not path.is_file():
            fail(
                "path_is_directory" if path.is_dir() else "file_not_found",
                "Read requires an existing file; use list to select one.",
            )
        if args.get("fields") is not None:
            if path.stat().st_size > MAX_FILE_BYTES:
                fail(
                    "field_selection_source_too_large",
                    "Use character pages for files larger than 1 MiB.",
                )
            return select_fields(path.read_bytes(), args["fields"], path)
        limit = integer(args.get("limit", 4000), "limit", 1, 8000)
        result = operational_state.read_page(path, offset, limit)
        if isinstance(result, dict) and result.get("end") is not None:
            result["next_offset"] = None if result.get("eof") else result["end"]
        return result
    except ToolInputError as error:
        return {"error": error.code, "hint": error.hint}
    except (FileNotFoundError, PermissionError, IsADirectoryError, UnicodeError):
        return {
            "error": "file_unavailable",
            "hint": "Check the path, file type and readable roots with list.",
        }
