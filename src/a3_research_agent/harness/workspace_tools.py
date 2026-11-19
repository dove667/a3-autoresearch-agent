"""Search, edit and delete temporary files within the workspace boundary."""

import hashlib
import os
import tempfile
from pathlib import Path

from a3_research_agent.path_safety import workspace_path

from . import paths, state_paths
from .tool_contracts import ToolInputError, arguments, boolean, definition, fail, integer, text

MAX_FILE_BYTES = 1024 * 1024
MAX_SEARCH_BYTES = 8 * 1024 * 1024
SKIP_DIRECTORIES = {".git", "__pycache__"}
STRING = {"type": "string"}
SEARCH_FIELDS = {
    "path": STRING,
    "query": {"type": "string", "minLength": 1, "maxLength": 200},
    "case_sensitive": {"type": "boolean"},
    "max_matches": {"type": "integer", "minimum": 1, "maximum": 40},
    "context_lines": {"type": "integer", "minimum": 0, "maximum": 3},
}
EDIT_FIELDS = {
    "path": STRING,
    "old_text": STRING,
    "new_text": STRING,
    "replace_all": {"type": "boolean"},
    "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
}
DELETE_FIELDS = {"path": STRING, "expected_sha256": EDIT_FIELDS["expected_sha256"]}
TOOLS = [
    definition(
        "search_files",
        "Find literal text in accessible UTF-8 files without reading whole files into context. "
        "Returns path, line, snippet and whole-file SHA256. Default path is the current workspace, "
        "case-insensitive; narrow path when results are truncated. Skips binary and oversized files.",
        SEARCH_FIELDS,
        ["query"],
    ),
    definition(
        "edit",
        "Replace one exact old_text in a workspace file. Ambiguous matches leave the file unchanged; "
        "set replace_all=true deliberately for all occurrences. Optional expected_sha256 rejects "
        "stale edits. Returns new SHA256; use write for a new file.",
        EDIT_FIELDS,
        ["path", "old_text", "new_text"],
    ),
    definition(
        "delete_scratch",
        "Delete one temporary text file under workspace-relative scratch/. "
        "Confirm it is no longer needed and supply its current SHA256 from read. "
        "No directory or symlink deletion.",
        DELETE_FIELDS,
        ["path", "expected_sha256"],
    ),
]
NAMES = frozenset(tool["name"] for tool in TOOLS)


def accessible_path(value, workspace, readonly_roots, *, writable=False):
    name = text(value, "path")
    path = (workspace / name).resolve()
    roots = [workspace] if writable else [workspace, *readonly_roots]
    if not any(path == root or path.is_relative_to(root) for root in roots):
        fail(
            "path_not_accessible",
            "Use a path inside the workspace or a deployed read-only input root.",
        )
    return path


def label(path, workspace):
    return path.relative_to(workspace).as_posix() if path.is_relative_to(workspace) else str(path)


def candidates(path):
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        fail(
            "path_not_found",
            "Select an existing file or directory; use list to inspect its parent.",
        )
    for root, directories, files in os.walk(path, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name not in SKIP_DIRECTORIES and not (Path(root) / name).is_symlink()
        )
        for name in sorted(files):
            source = Path(root) / name
            if not source.is_symlink():
                yield source


def search(args, workspace, readonly_roots):
    arguments(args, SEARCH_FIELDS)
    query = text(args.get("query"), "query", 200)
    sensitive = boolean(args.get("case_sensitive", False), "case_sensitive")
    maximum = integer(args.get("max_matches", 20), "max_matches", 1, 40)
    context = integer(args.get("context_lines", 0), "context_lines", 0, 3)
    path = accessible_path(args.get("path", "."), workspace, readonly_roots)
    needle = query if sensitive else query.casefold()
    result = {
        "query": query,
        "path": label(path, workspace),
        "matches": [],
        "scanned_files": 0,
        "skipped_files": 0,
        "truncated": False,
    }
    total = 0
    for count, source in enumerate(candidates(path)):
        if count >= 2000 or total >= MAX_SEARCH_BYTES:
            result["truncated"] = True
            break
        try:
            if source.stat().st_size > MAX_FILE_BYTES:
                result["skipped_files"] += 1
                continue
            raw = source.read_bytes()
            total += len(raw)
            if len(raw) > MAX_FILE_BYTES or total > MAX_SEARCH_BYTES:
                result["truncated"] = True
                break
            content = raw.decode("utf-8")
        except (OSError, UnicodeError):
            result["skipped_files"] += 1
            continue
        result["scanned_files"] += 1
        digest = None
        lines = content.splitlines()
        for index, line in enumerate(lines):
            if needle not in (line if sensitive else line.casefold()):
                continue
            if len(result["matches"]) >= maximum:
                result["truncated"] = True
                return result
            digest = digest or hashlib.sha256(raw).hexdigest()
            snippet = "\n".join(lines[max(0, index - context) : index + context + 1])
            result["matches"].append(
                {
                    "path": label(source, workspace),
                    "line": index + 1,
                    "text": snippet[:1200],
                    "snippet_truncated": len(snippet) > 1200,
                    "sha256": digest,
                }
            )
    return result


def edit(args, workspace, readonly_roots, protected):
    arguments(args, EDIT_FIELDS)
    path = accessible_path(args.get("path"), workspace, readonly_roots, writable=True)
    if path in protected or any(path.is_relative_to(root) for root in protected if root.is_dir()):
        fail(
            "managed_state_read_only",
            "Use the plan or preflight tool to update its managed records.",
        )
    if not path.is_file():
        fail(
            "source_file_missing",
            "Select an existing regular file; use write to create a new file.",
        )
    if path.stat().st_size > MAX_FILE_BYTES:
        fail("edit_file_too_large", "Use a bounded remote processor for files larger than 1 MiB.")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    expected = args.get("expected_sha256")
    if expected is not None and expected != digest:
        return {
            "error": "revision_conflict",
            "path": label(path, workspace),
            "sha256": digest,
            "hint": "Read the current file before editing it.",
        }
    try:
        original = raw.decode("utf-8")
    except UnicodeError:
        fail("binary_edit_not_supported", "Use a format-aware processor for binary files.")
    old = text(args.get("old_text"), "old_text", MAX_FILE_BYTES)
    new = args.get("new_text")
    if not isinstance(new, str) or len(new.encode()) > MAX_FILE_BYTES:
        fail(
            "invalid_new_text",
            "Use replacement text no larger than 1 MiB; empty text deletes the match.",
        )
    replace_all = boolean(args.get("replace_all", False), "replace_all")
    count = original.count(old)
    if not count:
        return {
            "error": "edit_match_not_found",
            "path": label(path, workspace),
            "sha256": digest,
            "hint": "Read the current text and supply an exact old_text match.",
        }
    if count != 1 and not replace_all:
        return {
            "error": "ambiguous_edit",
            "matches": count,
            "sha256": digest,
            "hint": "Include more surrounding text or deliberately set replace_all=true.",
        }
    updated = original.replace(old, new, -1 if replace_all else 1).encode()
    if len(updated) > MAX_FILE_BYTES:
        fail("edit_file_too_large", "Keep the edited file within 1 MiB.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != raw:
            return {
                "error": "revision_conflict",
                "path": label(path, workspace),
                "hint": "The source changed while editing; read its current version.",
            }
        os.chmod(temporary, path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {
        "path": label(path, workspace),
        "status": "edited",
        "replacements": count,
        "previous_sha256": digest,
        "sha256": hashlib.sha256(updated).hexdigest(),
    }


def delete_scratch(args, workspace):
    arguments(args, DELETE_FIELDS)
    name = text(args.get("path"), "path")
    expected = text(args.get("expected_sha256"), "expected_sha256", 64)
    try:
        path = workspace_path(workspace, name)
    except ValueError:
        fail("not_a_scratch_file", "Select a regular file under scratch/ without symlinks.")
    if not path.is_relative_to(workspace / "scratch") or not path.is_file():
        fail("not_a_scratch_file", "Select an existing regular file under scratch/.")
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES or b"\0" in raw:
        fail("not_a_scratch_text_file", "Select a temporary UTF-8 file up to 1 MiB.")
    try:
        raw.decode("utf-8")
    except UnicodeError:
        fail("not_a_scratch_text_file", "Select a temporary UTF-8 file up to 1 MiB.")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected:
        fail("revision_conflict", "Read the current file before deleting it.")
    path.unlink()
    return {"path": label(path, workspace), "status": "deleted", "sha256": digest}


def dispatch(name, args, *, workspace=None, readonly_roots=None, protected=()):
    root = Path(workspace or Path.cwd()).resolve()
    readonly = [
        Path(p).resolve()
        for p in (
            (
                (paths.reference_root(), paths.input_root(), state_paths.state_root())
                if name == "search_files"
                else ()
            )
            if readonly_roots is None
            else readonly_roots
        )
    ]
    try:
        if name == "search_files":
            return search(args, root, readonly)
        if name == "edit":
            return edit(args, root, readonly, [Path(p).resolve() for p in protected])
        if name == "delete_scratch":
            return delete_scratch(args, root)
        fail("unknown_workspace_tool", "Use search_files, edit or delete_scratch.")
    except ToolInputError as error:
        return {"error": error.code, "hint": error.hint}
