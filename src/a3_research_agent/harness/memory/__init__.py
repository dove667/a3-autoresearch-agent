"""Controlled, versioned research lessons with hybrid local retrieval."""

import time
import uuid

from a3_research_agent.harness import semantic_index

from . import retrieval, store

_FIELDS = {"text", "kind", "scope", "refs", "supersedes", "lifetime", "run_ids", "tags"}


class MemoryError(ValueError):
    def __init__(self, code, hint, **details):
        self.result = {"error": code, "hint": hint, **details}
        super().__init__(hint)


def text(value, field, limit, *, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise MemoryError("memory_validation", f"{field} must be text, at most {limit} characters")
    return value.strip()


def strings(value, field, count, length):
    if not isinstance(value, list) or len(value) > count:
        raise MemoryError("memory_validation", f"{field} must contain at most {count} strings")
    return list(dict.fromkeys(text(item, field, length, required=True) for item in value))


def integer(value, field, low, high):
    if type(value) is not int or not low <= value <= high:
        raise MemoryError("memory_validation", f"{field} must be an integer from {low} to {high}")
    return value


def fields(arguments, previous=None, *, run_id):
    values = {
        "text": "",
        "kind": "note",
        "scope": "",
        "refs": [],
        "supersedes": "",
        "lifetime": "durable",
        "run_ids": [],
        "tags": [],
        **{key: value for key, value in (previous or {}).items() if key in _FIELDS},
    }
    changes = {key: value for key, value in arguments.items() if key in _FIELDS}
    if "content" in arguments:
        changes["text"] = arguments["content"]
    values.update(changes)
    for key, maximum in (("text", 6000), ("kind", 48), ("scope", 600), ("supersedes", 64)):
        values[key] = text(values[key], key, maximum, required=key == "text")
    values["refs"] = strings(values["refs"], "refs", 16, 600)
    values["tags"] = strings(values["tags"], "tags", 24, 80)
    values["run_ids"] = strings(values["run_ids"], "run_ids", 64, 160)
    if values["lifetime"] not in ("durable", "run"):
        raise MemoryError("memory_validation", "lifetime must be durable or run")
    if not previous and run_id and run_id not in values["run_ids"]:
        values["run_ids"].append(run_id)
    if values["lifetime"] == "run" and not values["run_ids"]:
        raise MemoryError("memory_validation", "Run-lifetime notes require a run ID")
    return values


def exposed(entry, entries, run_id=""):
    return {
        "applicable": store.applicable(entry, run_id),
        **entry,
        "superseded_by": store.superseded_by(entries, run_id).get(entry["id"], []),
        "applicability": "Across runs; verify scope"
        if entry["lifetime"] == "durable"
        else "Only listed run_ids",
    }


def summary(entry, entries, *, run_id="", preview=300, score=None, similarity=None):
    result = {
        key: entry[key]
        for key in (
            "id",
            "revision",
            "sha256",
            "kind",
            "scope",
            "refs",
            "status",
            "lifetime",
            "run_ids",
            "created_run",
            "tags",
        )
    }
    result.update(
        applicable=store.applicable(entry, run_id),
        preview=entry["text"][:preview],
        superseded_by=store.superseded_by(entries, run_id).get(entry["id"], []),
    )
    if score is not None:
        result["score"] = round(score, 4)
    if similarity is not None:
        result["semantic_similarity"] = round(similarity, 4)
    return result


def selected(arguments, entries, run_id):
    include_archived = arguments.get("include_archived", False)
    if type(include_archived) is not bool:
        raise MemoryError("memory_validation", "include_archived must be boolean")
    kind = text(arguments.get("kind", ""), "kind", 48).casefold()
    scope = semantic_index.normalize(text(arguments.get("scope", ""), "scope", 600))
    hidden = store.superseded_by(entries, run_id)
    return [
        entry
        for entry in entries.values()
        if (store.applicable(entry, run_id) or include_archived)
        and (include_archived or (entry["status"] == "active" and entry["id"] not in hidden))
        and (not kind or kind == entry["kind"].casefold())
        and (not scope or scope in semantic_index.normalize(entry["scope"]))
    ]


def identify(arguments, entries, run_id):
    key = text(arguments.get("id", ""), "id", 64, required=True)
    matches = [entry for identifier, entry in entries.items() if identifier == key]
    if not matches:
        matches = [entry for identifier, entry in entries.items() if identifier.startswith(key)]
    if len(matches) != 1:
        raise MemoryError("memory_not_found", "Use an unambiguous ID from memory search/list")
    entry = matches[0]
    if not store.applicable(entry, run_id) and not arguments.get("include_archived", False):
        raise MemoryError(
            "memory_not_applicable",
            "This note is limited to another run",
            lifetime=entry["lifetime"],
            run_ids=entry["run_ids"],
        )
    return entry


def check_revision(arguments, entry):
    revision = arguments.get("revision")
    if type(revision) is not int or revision != entry["revision"]:
        raise MemoryError(
            "memory_conflict",
            "Read the note and supply its current revision before editing.",
            revision=entry["revision"],
        )


def validate_supersession(candidate, entries):
    previous = candidate["supersedes"]
    visited = {candidate["id"]}
    while previous:
        if previous in visited:
            raise MemoryError("memory_validation", "Memory supersession cannot contain a cycle")
        if previous not in entries:
            raise MemoryError("unknown_superseded_memory", "Use an existing note ID in supersedes")
        visited.add(previous)
        previous = entries[previous].get("supersedes", "")


def duplicate_candidates(candidate, entries, *, root, run_id, embedder, model_id):
    candidates = [
        entry
        for entry in selected({}, entries, run_id)
        if entry["id"] not in (candidate["id"], candidate.get("supersedes"))
    ]
    if embedder is None or not candidates:
        return []
    cache = semantic_index.VectorCache(root, embedder, model_id)
    vectors = cache.vectors(
        [retrieval.document(candidate), *[retrieval.document(entry) for entry in candidates]]
    )
    matches = [
        (entry, semantic_index.cosine(vectors[0], vector))
        for entry, vector in zip(candidates, vectors[1:])
    ]
    return [
        summary(entry, entries, run_id=run_id, score=similarity, similarity=similarity)
        for entry, similarity in sorted(matches, key=lambda match: match[1], reverse=True)
        if similarity >= 0.94
    ][:3]


def warm_index(root, embedder, model_id):
    with store.locked(root) as directory:
        entries = [
            entry for entry in store.current(directory).values() if entry["status"] == "active"
        ]
        semantic_index.VectorCache(directory, embedder, model_id).vectors(
            [retrieval.document(entry) for entry in entries]
        )
        return {"indexed": len(entries), "model": model_id}


def dispatch(arguments, *, root, run_id="", embedder=None, model_id="none", source_reader=None):
    try:
        if not isinstance(arguments, dict):
            raise MemoryError("memory_validation", "Memory arguments must be an object")
        note_fields = {
            "content",
            "kind",
            "scope",
            "refs",
            "supersedes",
            "lifetime",
            "run_ids",
            "tags",
            "force",
            "reason",
        }
        actions = {
            "write": note_fields,
            "update": note_fields | {"id", "revision"},
            "archive": {"id", "revision", "reason"},
            "restore": {"id", "revision", "reason"},
            "read": {"id", "include_archived"},
            "search": {"query", "kind", "scope", "include_archived", "offset", "limit"},
            "list": {"kind", "scope", "include_archived", "offset", "limit"},
            "context": {"query", "limit"},
            "sources": {"id", "ref", "offset", "limit", "include_archived"},
        }
        action = arguments.get("action", "read")
        if (
            not isinstance(action, str)
            or action not in actions
            or arguments.keys() - actions[action] - {"action"}
        ):
            raise MemoryError(
                "memory_validation", "Use only the fields for the selected memory action"
            )
        with store.locked(root) as directory:
            return _dispatch(
                arguments,
                root=directory,
                run_id=run_id,
                embedder=embedder,
                model_id=model_id,
                source_reader=source_reader,
            )
    except MemoryError as error:
        return error.result
    except (ValueError, TypeError) as error:
        return {"error": "memory_validation", "hint": str(error)}


def _dispatch(arguments, *, root, run_id, embedder, model_id, source_reader):
    action = arguments.get("action", "read")
    entries = store.current(root)
    if action == "restore":
        action = "update"
        arguments = {**arguments, "action": action, "status": "active"}
    if action in ("list", "search", "context"):
        query = text(arguments.get("query", ""), "query", 1200)
        candidates = selected(arguments, entries, run_id)
        ranked = retrieval.rank(
            candidates,
            query if action != "list" else "",
            root=root,
            embedder=embedder,
            model_id=model_id,
        )
        limit = integer(
            arguments.get("limit", 3 if action == "context" else 6),
            "limit",
            1,
            3 if action == "context" else 30,
        )
        offset = integer(arguments.get("offset", 0), "offset", 0, 1000000)
        items = [
            summary(
                entry,
                entries,
                run_id=run_id,
                preview=180 if action == "context" else 300,
                score=score,
                similarity=similarity,
            )
            for entry, score, similarity in ranked[offset : offset + limit]
        ]
        if action == "context":
            items = [
                {
                    "id": item["id"],
                    "preview": item["preview"],
                    "kind": item["kind"],
                    "scope": item["scope"][:180],
                    "refs": [ref[:180] for ref in item["refs"][:2]],
                    "lifetime": item["lifetime"],
                }
                for item in items
            ]
        return {
            "items": items,
            "total": len(ranked),
            "next_offset": offset + limit if offset + limit < len(ranked) else None,
            "retrieval": "hybrid" if embedder is not None else "lexical",
            "notice": "Read relevant notes and check scope/sources before applying. Plan tracks next work; skills store reusable procedures.",
        }
    if action == "read":
        return exposed(identify(arguments, entries, run_id), entries, run_id)
    if action == "sources":
        entry = identify(arguments, entries, run_id)
        if source_reader is None:
            return {
                "refs": entry["refs"],
                "hint": "Use the memory service to resolve source contents",
            }
        return source_reader(entry, arguments)
    if action not in ("write", "update", "archive"):
        raise MemoryError(
            "memory_validation", "Use write, read, list, search, update, archive or sources"
        )
    previous = None
    if action != "write":
        previous = identify(arguments, entries, run_id)
        check_revision(arguments, previous)
    reason = text(arguments.get("reason", ""), "reason", 1200)
    if action == "archive":
        if not reason:
            raise MemoryError("memory_validation", "Give reason when archiving a note")
        event = {
            **previous,
            "revision": previous["revision"] + 1,
            "status": "archived",
            "reason": reason,
            "ts": time.time(),
            "updated_run": run_id,
        }
        return exposed(store.append(root, event), entries, run_id)
    values = fields(arguments, previous, run_id=run_id)
    if previous is None:
        exact_fields = _FIELDS - {"run_ids"}
        exact = next(
            (
                entry
                for entry in selected({}, entries, run_id)
                if all(entry.get(key) == values[key] for key in exact_fields)
            ),
            None,
        )
        if exact:
            return {**exposed(exact, entries, run_id), "duplicate": True}
    event = {
        **(previous or {}),
        **values,
        "id": previous["id"] if previous else uuid.uuid4().hex[:12],
        "revision": previous["revision"] + 1 if previous else 1,
        "status": previous["status"] if previous else "active",
        "created_run": (previous["created_run"] or run_id) if previous else run_id,
        "updated_run": run_id,
        "ts": time.time(),
        "reason": reason,
    }
    if previous and "status" in arguments:
        if arguments["status"] != "active" or not reason:
            raise MemoryError(
                "memory_validation",
                "Use archive to close a note; restore with status=active and reason",
            )
        event["status"] = "active"
    validate_supersession(event, entries)
    similar = duplicate_candidates(
        event, entries, root=root, run_id=run_id, embedder=embedder, model_id=model_id
    )
    force = arguments.get("force", False)
    if type(force) is not bool:
        raise MemoryError("memory_validation", "force must be boolean")
    if similar and not (force and reason):
        raise MemoryError(
            "similar_memory_exists",
            "Read these notes; update the matching ID, supersede a wrong conclusion, or use force=true with reason for distinct scope",
            candidates=similar,
        )
    return exposed(store.append(root, event), entries, run_id)
