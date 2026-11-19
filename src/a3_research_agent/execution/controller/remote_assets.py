"""Inspect the configured dataset and model roots without starting a job."""

import json
import os
import stat
from pathlib import Path, PurePosixPath

from a3_research_agent.execution.exports.readonly import open_directory, open_source

from .artifacts import read_text_page

OPERATIONS = {
    "list": {"path", "offset", "limit"},
    "stat": {"path"},
    "search": {"query", "offset", "limit"},
    "read": {"path", "offset", "limit"},
}
ROOTS = {"datasets": "datasets_root", "models": "models_root"}
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_CATALOG_BYTES = 8 * 1024 * 1024
CATALOG = "a3-migration.json"


def relative_path(value, *, allow_root=False):
    if value == "" and allow_root:
        return ""
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError
    if "\\" in value or any(ord(character) < 32 for character in value):
        raise ValueError
    if any(not component or component.startswith(".") for component in value.split("/")):
        raise ValueError
    return PurePosixPath(value).as_posix()


def inspect_assets(request, settings):
    action, selection = request.get("action"), request.get("root")
    if (
        not isinstance(action, str)
        or action not in OPERATIONS
        or not isinstance(selection, str)
        or selection not in ROOTS
        or set(request) - (OPERATIONS[action] | {"action", "root"})
    ):
        return error("invalid_asset_request", "Choose datasets or models and a supported action.")
    try:
        offset = request.get("offset", 0)
        maximum = 8000 if action == "read" else 100
        limit = request.get("limit", 4000 if action == "read" else 30)
        if type(offset) is not int or not 0 <= offset <= 2**31 - 1:
            raise ValueError
        if type(limit) is not int or not 1 <= limit <= maximum:
            raise ValueError
        path = (
            relative_path(request.get("path", ""), allow_root=action == "list")
            if action != "search"
            else ""
        )
        query = request.get("query")
        if action == "search" and (
            not isinstance(query, str) or not 1 <= len(query.strip()) <= 256
        ):
            raise ValueError
    except ValueError:
        return error(
            "invalid_asset_request",
            "Use a visible, root-relative path or a nonempty catalog query with valid pagination.",
        )

    setting = getattr(settings, ROOTS[selection], None)
    if setting is None:
        return error("asset_root_unconfigured", "This deployment has no configured asset root.")
    root = Path(setting)
    data_root = Path(settings.data_root).resolve()
    if (
        not root.is_absolute()
        or ".." in root.parts
        or not root.is_relative_to(data_root)
        or root == data_root
    ):
        return error(
            "asset_root_unconfigured", "The asset root must be a directory on the data disk."
        )
    try:
        target = root / path
        if action == "list":
            details = list_entries(target, offset, limit)
        elif action == "stat":
            details = metadata(target)
        elif action == "search":
            details = search_catalog(root, query.strip(), offset, limit)
        else:
            details = read_text(target, offset, limit)
    except (OSError, ValueError):
        return error(
            "asset_unavailable",
            "Use an existing regular file or directory under the selected root. Links are not followed.",
        )
    return {"root": selection, "root_path": str(root), "path": path, **details}


def error(code, hint):
    return {"error": code, "hint": hint}


def entry_type(mode):
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "special"


def metadata(path):
    with open_directory(path.parent) as descriptor:
        info = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
    return {"type": entry_type(info.st_mode), "bytes": info.st_size, "mtime_ns": info.st_mtime_ns}


def list_entries(path, offset, limit):
    with open_directory(path) as descriptor:
        names = sorted(name for name in os.listdir(descriptor) if not name.startswith("."))
        entries = []
        for name in names[offset : offset + limit]:
            try:
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            entries.append({"name": name, "type": entry_type(info.st_mode)})
    return page("entries", entries, offset, limit, len(names))


def page(key, items, offset, limit, total):
    return {
        key: items,
        "total": total,
        "next_offset": offset + limit if offset + limit < total else None,
    }


def search_catalog(root, query, offset, limit):
    try:
        with open_source(root / CATALOG) as stream:
            raw = stream.read(MAX_CATALOG_BYTES + 1)
    except FileNotFoundError:
        return error("asset_catalog_missing", "List the relevant direction directory instead.")
    if len(raw) > MAX_CATALOG_BYTES:
        return error("asset_catalog_too_large", "List the relevant direction directory instead.")
    document = json.loads(raw)
    if not isinstance(document, dict) or not isinstance(document.get("assets"), list):
        return error("invalid_asset_catalog", "List the relevant direction directory instead.")
    matches = []
    needle = query.casefold()
    for item in document["assets"]:
        if not isinstance(item, dict):
            continue
        try:
            path = relative_path(item.get("path"))
        except ValueError:
            continue
        source = item.get("source", "")
        source = source if isinstance(source, str) else ""
        if needle in path.casefold() or needle in source.casefold():
            matches.append({"path": path, **({"source": source[:1024]} if source else {})})
    return page("matches", matches[offset : offset + limit], offset, limit, len(matches))


def read_text(path, offset, limit):
    with open_source(path) as stream:
        if os.fstat(stream.fileno()).st_size > MAX_TEXT_BYTES:
            return error(
                "asset_too_large", "Use stat for large files or search for catalog entries."
            )
        if b"\0" in stream.peek(4096)[:4096]:
            return error("asset_not_text", "Use stat for binary files.")
        result = read_text_page(stream, offset, limit, MAX_TEXT_BYTES)
    if result["encoding"] != "utf-8":
        return error("asset_not_text", "Use stat for binary files.")
    return {**result, "next_offset": None if result["eof"] else result["end"]}
