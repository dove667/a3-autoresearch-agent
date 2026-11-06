"""Manage versioned skill text in the current workspace without executing it."""

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

MAX_BYTES = 128 * 1024
PROPERTIES = {
    "action": {"type": "string", "enum": ["list", "read", "save", "archive"]},
    "id": {"type": "string"},
    "query": {"type": "string"},
    "description": {"type": "string"},
    "content": {"type": "string"},
    "keywords": {"type": "array", "items": {"type": "string"}},
    "source": {"type": "string"},
    "worker_roles": {"type": "array", "items": {"type": "string", "enum": ["policy", "harness"]}},
    "expected_sha256": {"type": "string"},
    "offset": {"type": "integer", "minimum": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 8000},
    "include_archived": {"type": "boolean"},
}
TOOLS = [
    {
        "type": "function",
        "name": "skills",
        "strict": False,
        "description": (
            "Manage reusable workspace skills. list(query) searches summaries and reports unindexed "
            "SKILL.md files; read(id,offset,limit) returns actual text and SHA256. "
            "save(id,description,content,source,keywords) registers text and automatically versions it; "
            "updates require expected_sha256 from read. archive(id,expected_sha256) hides a skill "
            "while preserving revisions. Read matching skills before reusable work; save lessons "
            "with applicability and sources. Text only; no code execution or expanded permissions."
        ),
        "parameters": {
            "type": "object",
            "properties": PROPERTIES,
            "required": ["action"],
            "additionalProperties": False,
        },
    }
]


class SkillError(ValueError):
    pass


def checked_path(root, relative):
    path = PurePosixPath(relative)
    if (
        path.as_posix() != relative
        or path.is_absolute()
        or "\\" in relative
        or not path.parts
        or path.parts[0] != "skills"
        or any(p in {".", ".."} for p in path.parts)
    ):
        raise SkillError("invalid_skill_path")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise SkillError("symlink_rejected")
    return current


def atomic_write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked_catalog(root):
    directory = checked_path(root, "skills")
    directory.mkdir(exist_ok=True)
    lock = checked_path(root, "skills/.registry.lock")
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        index = checked_path(root, "skills/index.json")
        if index.exists():
            if index.stat().st_size > 1024 * 1024:
                raise SkillError("catalog_too_large")
            catalog = json.loads(index.read_text())
        else:
            catalog = {"catalog_version": "1.0.0", "skills": []}
        if not isinstance(catalog, dict) or not isinstance(catalog.get("skills"), list):
            raise SkillError("invalid_catalog")
        entries = catalog["skills"]
        if any(not isinstance(e, dict) or not isinstance(e.get("id"), str) for e in entries):
            raise SkillError("invalid_catalog_entry")
        if len({e["id"] for e in entries}) != len(entries):
            raise SkillError("duplicate_catalog_id")
        yield catalog, index


def content_for(root, entry):
    source = entry.get("local_source")
    if not isinstance(source, str) or not source.startswith("skills/"):
        raw = json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()
        return "", hashlib.sha256(raw).hexdigest()
    path = checked_path(root, source)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise SkillError("missing_or_oversized_skill")
    raw = path.read_bytes()
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()


def metadata(entry):
    return {k: entry.get(k) for k in ("id", "version", "status", "local_source", "sha256")}


def dispatch(args, *, root=None):
    """Return bounded data/errors; mutations are locked and compare existing content."""
    try:
        return operate(Path(root or Path.cwd()).resolve(), args)
    except SkillError as error:
        return {"error": str(error)}
    except (OSError, ValueError, TypeError, KeyError):
        return {
            "error": "invalid_skill_request_or_storage",
            "hint": "Check fields, paths and catalog; existing revisions are preserved.",
        }


def operate(root, args):
    if not isinstance(args, dict) or set(args) - PROPERTIES.keys():
        raise SkillError("invalid_arguments")
    action = args.get("action")
    pending_write = None
    offset, limit = args.get("offset", 0), args.get("limit", 4000)
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 8000:
        raise SkillError("invalid_page")
    with locked_catalog(root) as (catalog, index):
        entries = catalog["skills"]
        if action == "list":
            query = args.get("query", "")
            if not isinstance(query, str):
                raise SkillError("invalid_query")
            query = query.casefold().strip()
            matches = [
                e
                for e in entries
                if (args.get("include_archived") or e.get("status") != "archived")
                and query
                in " ".join([e["id"], e.get("description", ""), *e.get("keywords", [])]).casefold()
            ]
            indexed = {e.get("local_source") for e in entries}
            unindexed = []
            for count, path in enumerate((root / "skills").rglob("SKILL.md")):
                if count >= 2000:
                    break
                name = path.relative_to(root).as_posix()
                if "/revisions/" not in name and name not in indexed and not path.is_symlink():
                    checked_path(root, name)
                    unindexed.append(name)
            return {
                "skills": [
                    {**metadata(e), "description": e.get("description", "")[:256]}
                    for e in matches[offset : offset + 24]
                ],
                "total": len(matches),
                "next_offset": offset + 24 if offset + 24 < len(matches) else None,
                "unindexed": unindexed[:24],
            }
        skill_id = args.get("id", "")
        if not isinstance(skill_id, str):
            raise SkillError("invalid_skill_id")
        entry = next((e for e in entries if e["id"] == skill_id), None)
        # Host-installed references include semantic versions in their existing IDs.
        pattern = r"[a-z0-9][a-z0-9.-]{0,159}" if entry else r"[a-z0-9][a-z0-9-]{0,79}"
        if not re.fullmatch(pattern, skill_id):
            raise SkillError("invalid_skill_id")
        if action == "read":
            if entry is None:
                raise SkillError("unknown_skill")
            text, digest = content_for(root, entry)
            end = min(len(text), offset + limit)
            return {
                **metadata(entry),
                "sha256": digest,
                "content": text[offset:end],
                "offset": offset,
                "end": end,
                "total": len(text),
                "eof": end >= len(text),
            }
        if action not in {"save", "archive"}:
            raise SkillError("invalid_action")
        if entry is not None:
            _, digest = content_for(root, entry)
            if args.get("expected_sha256") != digest:
                return {
                    "error": "revision_conflict",
                    "current_sha256": digest,
                    "hint": "Read the current skill before updating or archiving.",
                }
        elif args.get("expected_sha256") or action == "archive":
            raise SkillError("unknown_skill")
        if entry and (
            entry.get("status") == "local-function" or entry.get("id") == "synthetic-report-checker"
        ):
            raise SkillError("fixed_executable_entry_read_only")
        if action == "archive":
            entry["sha256"] = digest
            entry["status"] = "archived"
            result = metadata(entry)
        else:
            content, description, source = (
                args.get(k) for k in ("content", "description", "source")
            )
            if any(not isinstance(x, str) or not x.strip() for x in (content, description, source)):
                raise SkillError("content_description_source_required")
            if len(content.encode()) > MAX_BYTES or len(description) > 512 or len(source) > 2000:
                raise SkillError("skill_too_large")
            keywords = args.get("keywords", [])
            if (
                not isinstance(keywords, list)
                or len(keywords) > 24
                or any(not isinstance(k, str) or len(k) > 80 for k in keywords)
            ):
                raise SkillError("invalid_keywords")
            previous = entry.get("version", "0.0.0") if entry else "0.0.0"
            match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", previous)
            version = f"{match[1]}.{match[2]}.{int(match[3]) + 1}" if match else "0.1.0"
            if previous == "0.0.0":
                version = "0.1.0"
            digest = hashlib.sha256(content.encode()).hexdigest()
            relative = entry.get("local_source") if entry else None
            if not isinstance(relative, str) or not relative.startswith("skills/"):
                relative = f"skills/{skill_id}/SKILL.md"
            if PurePosixPath(relative).suffix.lower() != ".md":
                raise SkillError("skill_body_must_be_markdown")
            path = checked_path(root, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            roles = args.get("worker_roles", entry.get("worker_roles", []) if entry else [])
            if not isinstance(roles, list) or any(
                role not in {"policy", "harness"} for role in roles
            ):
                raise SkillError("invalid_worker_roles")
            revision = checked_path(
                root, f"skills/{skill_id}/revisions/{version}-{digest}/SKILL.md"
            )
            revision.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(revision, content.encode())
            history = list(entry.get("version_history", [])) if entry else []
            if entry:
                previous_text, previous_digest = content_for(root, entry)
                previous_path = checked_path(
                    root, f"skills/{skill_id}/revisions/previous-{previous_digest}/SKILL.md"
                )
                previous_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(previous_path, previous_text.encode())
                history.append(
                    {
                        **{k: v for k, v in entry.items() if k != "version_history"},
                        "sha256": previous_digest,
                        "local_source": previous_path.relative_to(root).as_posix(),
                        "reference_base": str(path.parent.relative_to(root)),
                    }
                )
            pending_write = (path, content.encode(), path.read_bytes() if path.exists() else None)
            updated = {
                **{
                    k: entry[k]
                    for k in ("reference_id", "deployed_tree_sha256", "external_input_revision")
                    if entry and k in entry
                },
                "id": skill_id,
                "version": version,
                "status": "reference",
                "description": description,
                "keywords": keywords,
                "provenance": source,
                "local_source": relative,
                "sha256": digest,
                "source_sha256": digest,
                "managed_by": "skills",
                "worker_roles": roles,
                "version_history": history,
            }
            if entry:
                entries[entries.index(entry)] = updated
            else:
                entries.append(updated)
            result = metadata(updated)
        data = json.dumps(catalog, ensure_ascii=False, indent=2).encode()
        if len(data) > 1024 * 1024:
            raise SkillError("catalog_too_large")
        if pending_write:
            target, new_bytes, old_bytes = pending_write
            atomic_write(target, new_bytes)
        try:
            atomic_write(index, data)
        except OSError:
            if pending_write:
                if old_bytes is None:
                    target.unlink(missing_ok=True)
                else:
                    atomic_write(target, old_bytes)
            raise
        return {
            **result,
            "notice": "Workspace text saved; other workspaces and loaded Python tools are unchanged.",
        }
