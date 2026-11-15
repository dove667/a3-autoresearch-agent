"""Store deliberate research notes in the Host-configured project memory directory."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from a3_research_agent.harness import jsonl_store, paths


def memory_path(path=None):
    """Resolve explicit test storage or the shared project note file."""
    return Path(path) if path is not None else paths.memory_root() / "entries.jsonl"


def write_memory(text, *, kind="note", refs=None, scope="", supersedes="", path=None):
    if not isinstance(text, str) or not text.strip():
        return {"error": "memory text is empty"}
    body = text.strip()
    if len(body) > 3000 or not all(
        isinstance(value, str) and len(value) <= limit
        for value, limit in ((kind, 48), (scope, 240), (supersedes, 64))
    ):
        return {
            "error": "memory_bounds",
            "hint": "Keep content within 3000 characters and use concise kind/scope",
        }
    if refs is None:
        refs = []
    if (
        not isinstance(refs, list)
        or len(refs) > 8
        or any(not isinstance(ref, str) or not ref.strip() or len(ref) > 240 for ref in refs)
    ):
        return {
            "error": "memory_refs",
            "hint": "Use at most 8 source IDs or paths, up to 240 characters each",
        }
    fields = {
        "text": body,
        "kind": kind.strip() or "note",
        "refs": list(dict.fromkeys(ref.strip() for ref in refs)),
        "scope": scope.strip(),
        "supersedes": supersedes.strip(),
    }
    path = memory_path(path)
    with jsonl_store.LOCK:
        entries = load_memory(path)
        if fields["supersedes"] and not any(row["id"] == fields["supersedes"] for row in entries):
            return {
                "error": "unknown_superseded_memory",
                "hint": "Use the exact ID of an existing note",
            }
        for row in reversed(entries):
            if all(row.get(key, "") == value for key, value in fields.items()):
                return row
        digest = hashlib.sha256(
            json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        entry = {"id": digest[:12], "ts": time.time(), "sha256": digest, **fields}
        return jsonl_store.append_record(path, entry)


def load_memory(path=None):
    return [
        row
        for row in jsonl_store.load_records(memory_path(path))
        if isinstance(row.get("id"), str) and isinstance(row.get("text"), str) and row["text"]
    ]


def current_notes(entries):
    superseded = {
        entry["supersedes"] for entry in entries if isinstance(entry.get("supersedes"), str)
    }
    unique = {}
    for entry in entries:
        if entry["id"] not in superseded:
            unique[entry["id"]] = entry
    return list(unique.values())


def search_memory(query, *, kind="", scope="", path=None):
    if not all(isinstance(value, str) and len(value) <= 240 for value in (query, kind, scope)):
        return {
            "error": "memory_search_bounds",
            "hint": "Use short keywords and optional kind/scope",
        }
    terms = set(query.casefold().split())
    matches = []
    for index, entry in enumerate(current_notes(load_memory(path))):
        entry_kind = str(entry.get("kind", "note"))
        entry_scope = str(entry.get("scope", ""))
        refs = entry.get("refs") or []
        if kind and kind.casefold() != entry_kind.casefold():
            continue
        if scope and scope.casefold() not in entry_scope.casefold():
            continue
        searchable = " ".join([entry["text"], entry_kind, entry_scope, str(refs)]).casefold()
        count = sum(term in searchable for term in terms)
        if terms and not count:
            continue
        score = (count, bool(refs), index)
        matches.append(
            (
                score,
                {
                    "id": entry["id"],
                    "kind": entry_kind,
                    "scope": entry_scope,
                    "preview": entry["text"][:300],
                    "refs": refs,
                },
            )
        )
    matches.sort(key=lambda match: match[0], reverse=True)
    return [entry for _, entry in matches[:6]]


def read_memory(key=None, *, path=None, preview=120):
    entries = load_memory(path)
    if not entries:
        return "长期记忆为空：先用 memory 工具写入一条经验。"
    if not key:
        notes = current_notes(entries)[-12:]
        lines = [f"- {e['id']} [{e.get('kind', 'note')}] {e['text'][:preview]}" for e in notes]
        return "记忆条目（最新在前，请用 id 读取全文）：\n" + "\n".join(reversed(lines))
    needle = str(key).strip().casefold()
    matches = [e for e in entries if e["id"] == needle]
    if not matches:
        matches = [e for e in entries if e["id"].startswith(needle)]
    if not matches:
        matches = [e for e in current_notes(entries) if needle in e["text"].casefold()]
    if not matches:
        return f"无此记忆: {key}"
    return "\n\n".join(
        f"[{e['id']}] {e['text']}\n"
        + json.dumps(
            {field: e[field] for field in ("kind", "refs", "scope", "supersedes") if e.get(field)},
            ensure_ascii=False,
        )
        for e in matches[-5:]
    )


def memory_tool(arguments):
    if not isinstance(arguments, dict):
        return {"error": "memory 参数必须是对象"}
    action = str(arguments.get("action") or "read").lower()
    if action == "write":
        return write_memory(
            arguments.get("content"),
            kind=arguments.get("kind", "note"),
            refs=arguments.get("refs"),
            scope=arguments.get("scope", ""),
            supersedes=arguments.get("supersedes", ""),
        )
    if action == "search":
        return search_memory(
            arguments.get("key", ""),
            kind=arguments.get("kind", ""),
            scope=arguments.get("scope", ""),
        )
    if action in ("read", "list"):
        return read_memory(arguments.get("key") if action == "read" else None)
    return {"error": "unknown memory action; use write / read / list / search"}
