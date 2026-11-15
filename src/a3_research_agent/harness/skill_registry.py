"""Expose progressive discovery and controlled edits of the project skill library."""

import os
import re
from pathlib import Path

from . import skill_library as library

ACTIONS = [
    "list",
    "read",
    "save",
    "edit",
    "edit_resource",
    "archive",
    "list_resources",
    "read_resource",
    "write_resource",
    "delete_resource",
]
PROPERTIES = {
    "action": {"type": "string", "enum": ACTIONS},
    "id": {"type": "string"},
    "path": {"type": "string"},
    "query": {"type": "string", "maxLength": 240},
    "description": {"type": "string", "maxLength": 512},
    "content": {"type": "string"},
    "old_text": {"type": "string", "minLength": 1},
    "new_text": {"type": "string"},
    "replace_all": {"type": "boolean"},
    "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 24},
    "source": {"type": "string", "maxLength": 2000},
    "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
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
            "Use the single project skill library: list(query) returns summaries; read(id) reads a "
            "matching SKILL.md; list_resources(id) lists attachments; read_resource(id,path) reads "
            "only a needed UTF-8 attachment. Text reads use character offset/limit pages and return "
            "SHA256. save(id,description,content,source,keywords?) creates or updates a skill; updating "
            "and archive require the current body expected_sha256. save restores archived skills. "
            "edit(id,old_text,new_text,expected_sha256,replace_all=false) changes only Markdown body "
            "text, keeping managed YAML metadata intact. edit_resource adds path and edits a UTF-8 "
            "attachment using its resource hash. Exact text must match once unless replace_all=true. "
            "write_resource(id,path,content,expected_sha256?) creates an absent attachment without "
            "a hash; replacing or delete_resource requires its current resource SHA256. Every edit "
            "versions the whole package and preserves its previous files. No code execution."
        ),
        "parameters": {
            "type": "object",
            "properties": PROPERTIES,
            "required": ["action"],
            "additionalProperties": False,
        },
    }
]


def configured_roots(root, history_root):
    selected = root if root is not None else os.environ.get("A3_SKILLS_ROOT")
    if not selected or not Path(selected).is_absolute():
        raise library.SkillError("host_skills_root_required")
    root = Path(selected)
    if root.is_symlink():
        raise library.SkillError("symlink_rejected")
    root = root.resolve()
    selected_history = (
        history_root
        if history_root is not None
        else os.environ.get("A3_SKILLS_HISTORY_ROOT", root.parent / ".runtime/skill-revisions")
    )
    history_root = Path(selected_history)
    if not history_root.is_absolute() or history_root.is_symlink():
        raise library.SkillError("invalid_skill_history_root")
    history_root = history_root.resolve()
    if history_root.is_relative_to(root) or root.is_relative_to(history_root):
        raise library.SkillError("overlapping_skill_history_root")
    return root, history_root


def summary(entry):
    return {name: entry.get(name) for name in ("id", "version", "status", "description")}


def page(raw, entry, offset, limit, **extra):
    result = {**summary(entry), "sha256": library.digest(raw), "size_bytes": len(raw), **extra}
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return {
            **result,
            "encoding": "binary",
            "notice": "Binary resource; text was not delivered.",
        }
    start, end = min(offset, len(text)), min(offset + limit, len(text))
    return {
        **result,
        "encoding": "utf-8",
        "unit": "characters",
        "content": text[start:end],
        "offset": start,
        "end": end,
        "total": len(text),
        "eof": end >= len(text),
    }


def checked_text(value, maximum, error):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > maximum:
        raise library.SkillError(error)
    return value


def conflict(expected, raw):
    digest = library.digest(raw) if raw is not None else None
    if expected != digest:
        return {
            "error": "revision_conflict",
            "current_sha256": digest,
            "hint": "Read the current body/resource and use its SHA256 before changing it.",
        }
    return None


def replace_text(raw, args):
    """Return exact text changes without accepting ambiguous or binary edits."""
    try:
        current = raw.decode("utf-8")
    except UnicodeError as error:
        raise library.SkillError("binary_resource_not_editable") from error
    old, new = args.get("old_text"), args.get("new_text")
    replace_all = args.get("replace_all", False)
    if not isinstance(old, str) or not old or not isinstance(new, str):
        raise library.SkillError("edit_requires_old_and_new_text")
    if type(replace_all) is not bool:
        raise library.SkillError("invalid_replace_all")
    matches = current.count(old)
    if not matches:
        raise library.SkillError("edit_text_not_found")
    if matches > 1 and not replace_all:
        raise library.SkillError("edit_text_ambiguous")
    return current.replace(old, new, -1 if replace_all else 1), matches


def dispatch(args, *, root=None, history_root=None):
    try:
        root, history_root = configured_roots(root, history_root)
        if not isinstance(args, dict) or set(args) - PROPERTIES.keys():
            raise library.SkillError("invalid_arguments")
        offset, limit = args.get("offset", 0), args.get("limit", 4000)
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 8000
        ):
            raise library.SkillError("invalid_page")
        if type(args.get("include_archived", False)) is not bool:
            raise library.SkillError("invalid_archived_selection")
        with library.locked_library(root, history_root):
            return operate(root, history_root, args, offset, limit)
    except library.SkillError as error:
        return {"error": str(error)}
    except (OSError, ValueError, TypeError, KeyError):
        return {
            "error": "invalid_skill_request_or_storage",
            "hint": "Check skill metadata, paths and source; previous revisions are preserved.",
        }


def operate(root, history_root, args, offset, limit):
    action = args.get("action")
    if action == "list":
        query = args.get("query", "")
        if not isinstance(query, str) or len(query) > 240:
            raise library.SkillError("invalid_query")
        terms = set(re.findall(r"\w+", query.casefold()))
        matches = []
        for entry in library.catalogs(root):
            if not args.get("include_archived") and entry.get("status") == "archived":
                continue
            text = " ".join(
                [entry["id"], entry["description"], *entry.get("keywords", [])]
            ).casefold()
            score = sum(term in text for term in terms)
            if not terms or score:
                matches.append((score, entry))
        matches.sort(key=lambda item: item[0], reverse=True)
        return {
            "skills": [
                {**summary(e), "description": e["description"][:256]}
                for _, e in matches[offset : offset + 24]
            ],
            "total": len(matches),
            "next_offset": offset + 24 if offset + 24 < len(matches) else None,
        }
    skill_id = library.identifier(args.get("id"))
    entry = library.read_catalog(root, skill_id)
    if entry is None and action != "save":
        raise library.SkillError("unknown_skill")
    raw = library.read_body(root, entry) if entry else None
    if action == "read":
        return page(raw, entry, offset, limit)
    if action == "edit":
        if entry.get("status") == "archived":
            raise library.SkillError("archived_skill_requires_save")
        if error := conflict(args.get("expected_sha256"), raw):
            return error
        header, content = library.frontmatter(raw.decode("utf-8"))
        replacement, matches = replace_text(content.encode(), args)
        if replacement == content:
            return {**summary(entry), "sha256": library.digest(raw), "unchanged": True}
        updated = {**entry, "version": library.next_version(entry["version"])}
        # Preserve the managed header; the exact edit only applies to Markdown content.
        body = library.body_with_header(header, replacement, updated)
        revision = library.commit_package(root, history_root, updated, body)
        return {
            **summary(updated),
            "sha256": library.digest(body),
            "replacements": matches,
            "history_revision": revision,
        }
    if action == "list_resources":
        files = library.package_files(root, skill_id)
        resources = [
            {"path": name, "size_bytes": len(data)}
            for name, data in sorted(files.items())
            if name not in {library.BODY, library.CATALOG}
        ]
        return {
            **summary(entry),
            "resources": resources[offset : offset + 24],
            "total": len(resources),
            "next_offset": offset + 24 if offset + 24 < len(resources) else None,
        }
    if action in {"read_resource", "write_resource", "delete_resource", "edit_resource"}:
        relative = args.get("path")
        path = library.resource_path(root, skill_id, relative)
        resource = library.read_bytes(path, library.MAX_RESOURCE_BYTES) if path.exists() else None
        if action == "read_resource":
            if resource is None:
                raise library.SkillError("resource_not_found")
            return page(resource, entry, offset, limit, path=relative)
        if entry.get("status") == "archived":
            raise library.SkillError("archived_skill_requires_save")
        if error := conflict(args.get("expected_sha256"), resource):
            return error
        if action in {"delete_resource", "edit_resource"} and resource is None:
            raise library.SkillError("resource_not_found")
        replacement = None
        if action == "write_resource":
            content = args.get("content")
            if not isinstance(content, str) or len(content.encode()) > library.MAX_RESOURCE_BYTES:
                raise library.SkillError("resource_too_large")
            replacement = content.encode()
        matches = None
        if action == "edit_resource":
            content, matches = replace_text(resource, args)
            replacement = content.encode()
            if len(replacement) > library.MAX_RESOURCE_BYTES:
                raise library.SkillError("resource_too_large")
            if replacement == resource:
                return {
                    **summary(entry),
                    "path": relative,
                    "sha256": library.digest(resource),
                    "unchanged": True,
                }
        updated = {**entry, "version": library.next_version(entry["version"])}
        body = library.body_bytes(raw.decode(), updated)
        revision = library.commit_package(
            root, history_root, updated, body, {relative: replacement}
        )
        return {
            **summary(updated),
            "path": relative,
            "sha256": library.digest(replacement) if replacement is not None else None,
            "skill_sha256": library.digest(body),
            "history_revision": revision,
            **({"replacements": matches} if matches is not None else {}),
        }
    if action not in {"save", "archive"}:
        raise library.SkillError("invalid_action")
    if error := conflict(args.get("expected_sha256"), raw):
        return error
    if action == "archive":
        updated = {**entry, "status": "archived", "version": library.next_version(entry["version"])}
        body = library.body_bytes(raw.decode(), updated)
    else:
        content = checked_text(
            args.get("content"), library.MAX_BODY_BYTES, "skill_content_required"
        )
        description = checked_text(args.get("description"), 512, "skill_description_required")
        source = checked_text(args.get("source"), 2000, "skill_source_required")
        keywords = args.get("keywords", [])
        if (
            not isinstance(keywords, list)
            or len(keywords) > 24
            or any(not isinstance(word, str) or len(word) > 80 for word in keywords)
        ):
            raise library.SkillError("invalid_keywords")
        updated = {
            **(entry or {}),
            "id": skill_id,
            "status": "active",
            "version": library.next_version(entry["version"]) if entry else "0.1.0",
            "description": description,
            "keywords": keywords,
            "provenance": source,
            "local_source": f"skills/{skill_id}/{library.BODY}",
        }
        body = library.body_bytes(content, updated)
    revision = library.commit_package(root, history_root, updated, body)
    return {**summary(updated), "sha256": library.digest(body), "history_revision": revision}
