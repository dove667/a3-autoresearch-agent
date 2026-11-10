"""Durable memory and Recall for the recovery harness.

Two append-only files live in the workspace:

``history.jsonl``
    Every message the loop produced, written before compaction and never
    truncated. Compaction only rewrites what the *model* sees, so Recall can
    still return original evidence that no longer fits in the context window.

``memory.jsonl``
    Notes the agent deliberately keeps (decisions, dead ends, applicable
    conditions). Write is append-only; superseding an entry links to its original
    ID. Search shows current notes; original IDs remain readable.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import threading
import time
from collections import Counter

HISTORY_PATH = "history.jsonl"
MEMORY_PATH = "memory.jsonl"
SNIPPET_WINDOW = 100
SNIPPET_LIMIT = 30
_HISTORY_LOCK = threading.RLock()


def _append(path, payload):
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with _HISTORY_LOCK:
        needs_newline = False
        if target.exists() and target.stat().st_size:
            with target.open("rb") as previous:
                previous.seek(-1, os.SEEK_END)
                needs_newline = previous.read(1) != b"\n"
        with target.open("a", encoding="utf-8") as stream:
            if needs_newline:
                stream.write("\n")
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return payload


def _load(path):
    target = pathlib.Path(path)
    if not target.is_file():
        return []
    rows = []
    with target.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                # A truncated tail must not destroy earlier evidence.
                rows.append({"type": "corrupt_line", "line": number})
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


# ---------------------------------------------------------------- history ---


def append_history(message, path=HISTORY_PATH):
    """Persist one model-visible message verbatim, forever."""
    if not isinstance(message, dict):
        return None
    return _append(path, message)


def append_history_many(messages, path=HISTORY_PATH):
    written = 0
    for message in messages or []:
        if append_history(message, path) is not None:
            written += 1
    return written


def load_history(path=HISTORY_PATH):
    return [row for row in _load(path) if row.get("type") != "corrupt_line"]


def _archive_matches(path, expected):
    """Read only workspace files, never symlinked archive components."""
    if path.is_absolute():
        return False
    current = pathlib.Path.cwd()
    for part in path.parts:
        if part in {"", ".", ".."}:
            return False
        current = current / part
        if current.is_symlink():
            return False
    try:
        return current.is_file() and hashlib.sha256(current.read_bytes()).hexdigest().startswith(
            expected
        )
    except OSError:
        return False


def _collected_archive_paths(reference, expected):
    """Resolve legacy citations through collection receipts, never workspace scans."""
    ledger = pathlib.Path("orchestration/assignments.json")
    if any(part.is_symlink() for part in (ledger, *ledger.parents)):
        return []
    try:
        state = json.loads(ledger.read_text())
        if state.get("version") != 1 or not isinstance(state.get("assignments"), dict):
            return []
        candidates = []
        for identifier, item in state["assignments"].items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier) or not isinstance(item, dict):
                continue
            if (
                not item.get("session_id")
                or {**item.get("reference_hashes", {}), **item.get("evidence_hashes", {})}.get(
                    reference
                )
                != expected
            ):
                continue
            candidates.append(pathlib.Path("orchestration/results") / identifier / reference)
        return candidates
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def _verify_archive_reference(reference, allow_collected):
    path = pathlib.Path(reference)
    expected = path.stem.rsplit("-", 1)[-1]
    if reference.startswith("orchestration/results/"):
        relative = pathlib.PurePosixPath(*path.parts[3:]).as_posix()
        candidates = _collected_archive_paths(relative, expected)
        return path in candidates and _archive_matches(path, expected)
    if path.exists() or path.is_symlink():
        return _archive_matches(path, expected)
    if allow_collected and len(expected) == 64:
        candidates = _collected_archive_paths(reference, expected)
        # Ambiguous legacy citations require explicit provenance, not a guessed owner.
        return len(candidates) == 1 and _archive_matches(candidates[0], expected)
    return False


def _verify_control_sources(messages):
    references = {}
    for message in messages:
        content = message.get("content")
        # Only the program-written provenance header is a control dependency.
        # Model-authored notes and worker report citations are untrusted content,
        # not permission grants or evidence that a local archive was delivered.
        if isinstance(content, str):
            header = content
            if content.startswith("Untrusted derived working memory,"):
                _, marker, header = content.partition("<compacted-checkpoint>\n")
                if not marker or "</compacted-checkpoint>" not in header:
                    raise ValueError("Malformed control archive frame")
            line = header.split("\n", 1)[0]
            source = None
            if line.startswith("Working memory derived from archived untrusted conversation;"):
                source = re.search(r"Source: (\S+)", line)
            elif line.startswith("上下文压缩失败："):
                source = re.search(r"Read the lossless source (\S+?), or use Recall", line)
            recognized = line.startswith(
                ("Working memory derived from archived untrusted conversation;", "上下文压缩失败：")
            )
            if content.startswith("Untrusted derived working memory,") and not recognized:
                raise ValueError("Invalid framed control archive source header")
            if recognized and source is None:
                raise ValueError("Missing control archive source header")
            if source:
                reference = source.group(1)
                if not re.fullmatch(
                    r"(?:orchestration/results/[A-Za-z0-9_-]{1,80}/)?"
                    r"context-history/(?:snapshot|tool-output)-[0-9a-f]{64}\.json",
                    reference,
                ):
                    raise ValueError("Invalid control archive source header")
                references[reference] = True
        if message.get("type") == "function_call_output":
            try:
                preview = json.loads(message.get("output", ""))
                if isinstance(preview, str):
                    preview = json.loads(preview)
            except (ValueError, TypeError):
                continue
            if isinstance(preview, dict) and preview.get("preview_complete") is False:
                reference = preview.get("path")
                if isinstance(reference, str) and re.fullmatch(
                    r"context-history/tool-output-[0-9a-f]{64}\.json", reference
                ):
                    references[reference] = False
                artifact = preview.get("artifact")
                if isinstance(artifact, dict) and "path" in artifact:
                    path, sha = artifact.get("path"), artifact.get("sha256")
                    if (
                        not isinstance(path, str)
                        or not re.fullmatch(
                            r"context-history/artifact-[0-9a-f]{64}\.(?:txt|bin)", path
                        )
                        or pathlib.Path(path).stem.rsplit("-", 1)[-1] != sha
                    ):
                        raise ValueError("Invalid decoded artifact reference")
                    references[path] = False
            if isinstance(preview, dict) and preview.get("spilled") is True:
                reference = preview.get("path")
                if not isinstance(reference, str) or not re.fullmatch(
                    r"\.autorun-spill/[^/]+-(?:[0-9a-f]{16}|[0-9a-f]{64})\.txt", reference
                ):
                    raise ValueError("Invalid original spill reference; explicit recovery required")
                references[reference] = False
    for reference, allow_collected in references.items():
        if not _verify_archive_reference(reference, allow_collected):
            raise ValueError(
                "Referenced original archive is missing or corrupt; explicit recovery required"
            )
    return len(references)


def ensure_history(messages, path=HISTORY_PATH):
    """Reconcile recovered raw messages without duplicating existing occurrences.

    Recovery can add tool outputs absent from the append-only log. Index the
    actual existing messages (including multiplicity), never merely a receipt or
    summary reference. Torn records stay preserved and are reported as gaps.
    """
    if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
        raise ValueError("Recovered history must be a list of message objects")

    def identity(item):
        return hashlib.sha256(
            json.dumps(item, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    with _HISTORY_LOCK:
        source_checks = _verify_control_sources(messages)
        # Index only the checkpoint identities while streaming the lifetime log;
        # reconciliation must not load every historical tool result into memory.
        wanted = {identity(message) for message in messages}
        existing, corrupt_lines = Counter(), 0
        target = pathlib.Path(path)
        if target.exists():
            with target.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        corrupt_lines += 1
                        continue
                    if isinstance(row, dict):
                        key = identity(row)
                        if key in wanted:
                            existing[key] += 1
        seen, written = Counter(), 0
        for message in messages:
            key = identity(message)
            seen[key] += 1
            if seen[key] > existing[key]:
                append_history(message, path)
                written += 1
        return {
            "appended": written,
            "source_checks": source_checks,
            "corrupt_lines": corrupt_lines,
        }


# ----------------------------------------------------------------- rounds ---


def _content_text(message):
    """Flatten one message into searchable text, including tool traffic."""
    kind = message.get("type")
    if kind == "function_call":
        return f"{message.get('name', '')} {message.get('arguments', '')}".strip()
    if kind == "function_call_output":
        output = message.get("output", "")
        return output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    if kind == "reasoning":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def split_rounds(messages):
    """Split on completed decision batches as well as explicit user turns.

    Autonomous runs often have one initial user turn followed by many tool
    batches. Parallel calls and their outputs remain in the same round.
    """
    rounds, current, pending = [], [], set()
    completed_tools = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        kind = message.get("type")
        user_turn = message.get("role") == "user" and kind != "function_call_output"
        boundary = (
            not pending
            and current
            and (user_turn or (completed_tools and kind != "function_call_output"))
        )
        if boundary:
            rounds.append(current)
            current, completed_tools = [], False
        current.append(message)
        if kind == "function_call":
            pending.add(message.get("call_id"))
        elif kind == "function_call_output":
            pending.discard(message.get("call_id"))
            completed_tools = not pending
    if current:
        rounds.append(current)
    return rounds


def round_text(round_messages):
    return "\n".join(text for text in (_content_text(m) for m in round_messages) if text)


def recent_rounds(messages, count=5):
    """The last ``count`` rounds with their original text (fallback payload)."""
    rounds = split_rounds(messages)
    return rounds[-count:] if count > 0 else []


def recent_rounds_text(messages, count=5, limit_per_round=4000):
    blocks = []
    total = len(split_rounds(messages))
    for offset, round_messages in enumerate(recent_rounds(messages, count)):
        text = round_text(round_messages).strip()
        if not text:
            continue
        if len(text) > limit_per_round:
            text = text[:limit_per_round] + "\n[round truncated]"
        index = total - len(recent_rounds(messages, count)) + offset + 1
        blocks.append(f"--- round {index} ---\n{text}")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- recall ----


RETRIEVAL_TOOLS = frozenset({"recall", "memory", "plan", "progress"})


def readable_result(value):
    if isinstance(value, dict):
        return "\n".join(f"{key}: {readable_result(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(map(readable_result, value))
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def evidence_text(message, call_names, mode):
    if mode == "raw":
        return _content_text(message)
    kind = message.get("type")
    if kind == "function_call":
        return ""
    if kind == "function_call_output":
        if call_names.get(message.get("call_id")) in RETRIEVAL_TOOLS:
            return ""
        # Tool output is often a JSON string inside the Responses message.
        text = _content_text(message)
        try:
            value = json.loads(text)
        except ValueError:
            return text
        return readable_result(value)
    return _content_text(message)


def recall(
    keywords,
    n,
    m,
    *,
    messages=None,
    path=HISTORY_PATH,
    window=SNIPPET_WINDOW,
    limit=SNIPPET_LIMIT,
    mode="evidence",
):
    """Search original rounds n back through m back, with exact round labels."""
    terms = list(dict.fromkeys(str(k).strip()[:160] for k in (keywords or []) if str(k).strip()))
    if not terms:
        return ["keywords 不能为空：传入 1 个或多个关键字"]
    try:
        n_int, m_int = sorted((max(1, int(n)), max(1, int(m))), reverse=True)
        half = max(1, min(400, int(window)) // 2)
        limit = max(1, min(SNIPPET_LIMIT, int(limit)))
    except (TypeError, ValueError):
        return ["n、m、window、limit 必须为整数：1 表示最近一轮"]
    if mode not in ("evidence", "raw"):
        return ["mode 必须为 evidence 或 raw"]
    source = list(messages) if messages is not None else load_history(path)
    rounds = split_rounds(source)
    if not rounds:
        return ["history.jsonl 中没有可用历史（尚无记录或被清空）"]
    call_names = {
        message.get("call_id"): message.get("name")
        for message in source
        if message.get("type") == "function_call"
    }
    pattern = re.compile("|".join(map(re.escape, terms[:12])), re.IGNORECASE)
    candidates, seen = [], set()
    end = min(n_int, len(rounds))
    for ago in range(m_int, end + 1):
        for message in rounds[-ago]:
            original = evidence_text(message, call_names, mode)
            matches = pattern.finditer(original)
            match = next(matches, None)
            if match is None:
                continue
            matched_terms = {match.group().casefold()}
            if len(terms) > 1:
                for candidate in matches:
                    matched_terms.add(candidate.group().casefold())
                    if len(matched_terms) == len(terms):
                        break
            # Keep one representative snippet per message; prefer actual tool output.
            snippet = " ".join(original[max(0, match.start() - half) : match.end() + half].split())
            if snippet in seen:
                continue
            seen.add(snippet)
            is_output = message.get("type") == "function_call_output"
            candidates.append((len(matched_terms), is_output, -ago, snippet))
    if not candidates:
        return [f"在 {m_int}~{n_int} 轮前范围内未找到关键字：" + ", ".join(terms[:12])]
    candidates.sort(key=lambda hit: hit[:3], reverse=True)
    return [f"[{-ago}轮前] …{snippet}…" for _, _, ago, snippet in candidates[:limit]]


def recall_tool(arguments):
    if not isinstance(arguments, dict):
        return ["recall 参数必须是对象"]
    keywords = arguments.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    if not isinstance(keywords, list):
        return ["keywords 必须为文本数组"]
    return recall(
        keywords,
        arguments.get("n", 1),
        arguments.get("m", 1),
        window=arguments.get("window", SNIPPET_WINDOW),
        limit=arguments.get("limit", SNIPPET_LIMIT),
        mode=arguments.get("mode", "evidence"),
    )


# ---------------------------------------------------------------- memory ----


def write_memory(text, *, kind="note", refs=None, scope="", supersedes="", path=MEMORY_PATH):
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
    with _HISTORY_LOCK:
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
        return _append(path, entry)


def load_memory(path=MEMORY_PATH):
    return [
        row
        for row in _load(path)
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


def search_memory(query, *, kind="", scope="", path=MEMORY_PATH):
    if not all(isinstance(value, str) and len(value) <= 240 for value in (query, kind, scope)):
        return {
            "error": "memory_search_bounds",
            "hint": "Use short keywords and optional kind/scope",
        }
    terms = set(query.casefold().split())
    if not terms and not kind.strip() and not scope.strip():
        return {
            "error": "memory_search_empty",
            "hint": "Supply keywords in key or a kind/scope filter",
        }
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


def read_memory(key=None, *, path=MEMORY_PATH, preview=120):
    entries = load_memory(path)
    if not entries:
        return "memory.jsonl 为空：先用 memory 工具写入一条经验。"
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
