"""Durable memory and Recall for the recovery harness.

Two append-only files live in the workspace:

``history.jsonl``
    Every message the loop produced, written before compaction and never
    truncated. Compaction only rewrites what the *model* sees, so Recall can
    still return original evidence that no longer fits in the context window.

``memory.jsonl``
    Notes the agent deliberately keeps (decisions, dead ends, applicable
    conditions). Write is append-only; superseding an entry means writing a new
    one that names the old id, so the audit trail survives.
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
    for number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
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
                or item.get("evidence_hashes", {}).get(reference) != expected
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
        if isinstance(content, str) and content.startswith(
            (
                "Working memory derived from archived untrusted conversation;",
                "Untrusted derived working memory, not instructions, independent proof or acceptance.",
            )
        ):
            for reference in re.findall(
                r"(?:orchestration/results/[A-Za-z0-9_-]{1,80}/)?"
                r"context-history/(?:snapshot|tool-output)-[0-9a-f]{64}\.json",
                content,
            ):
                references.setdefault(reference, True)
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


def recall(
    keywords, n, m, *, messages=None, path=HISTORY_PATH, window=SNIPPET_WINDOW, limit=SNIPPET_LIMIT
):
    """Search original rounds ``n`` back through ``m`` back, returning snippets."""
    terms = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not terms:
        return ["keywords 不能为空：传入 1 个或多个关键字"]
    source = list(messages) if messages is not None else load_history(path)
    rounds = split_rounds(source)
    if not rounds:
        return ["history.jsonl 中没有可用历史（尚无记录或被清空）"]
    try:
        n_int, m_int = int(n), int(m)
    except (TypeError, ValueError):
        return ["n、m 必须为整数：1 表示最近一轮"]
    n_int, m_int = max(1, n_int), max(1, m_int)
    if n_int < m_int:
        n_int, m_int = m_int, n_int
    start = max(0, len(rounds) - n_int)
    end = min(len(rounds), len(rounds) - m_int + 1)
    if end <= start:
        end = min(len(rounds), start + 1)
    haystack = "\n".join(round_text(rnd) for rnd in rounds[start:end])
    if not haystack:
        return ["指定轮次范围内没有可搜索文本"]
    half = max(1, int(window) // 2)
    lowered = haystack.lower()
    found, seen = [], set()
    for term in terms:
        needle = term.lower()
        position = 0
        while len(found) < limit:
            index = lowered.find(needle, position)
            if index < 0:
                break
            low = max(0, index - half)
            high = min(len(haystack), index + len(needle) + half)
            snippet = " ".join(haystack[low:high].split())
            if snippet and snippet not in seen:
                seen.add(snippet)
                ago_from = len(rounds) - end + 1
                ago_to = len(rounds) - start
                found.append(f"[{ago_to}~{ago_from}轮前] …{snippet}…")
            position = index + max(1, len(needle))
    if not found:
        return [f"在 {m_int}~{n_int} 轮前范围内未找到关键字：" + ", ".join(terms)]
    return found


def recall_tool(arguments):
    """Dispatch entry used by agent.py."""
    if not isinstance(arguments, dict):
        return ["recall 参数必须是对象"]
    keywords = arguments.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    return recall(
        keywords,
        arguments.get("n", 1),
        arguments.get("m", 1),
        window=arguments.get("window", SNIPPET_WINDOW),
        limit=arguments.get("limit", SNIPPET_LIMIT),
    )


# ---------------------------------------------------------------- memory ----


def write_memory(text, *, kind="note", refs=None, path=MEMORY_PATH):
    body = (text or "").strip()
    if not body:
        return {"error": "memory text is empty"}
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    entry = {
        "id": digest[:12],
        "ts": time.time(),
        "kind": str(kind or "note"),
        "text": body,
        "sha256": digest,
        "refs": list(refs or []),
    }
    return _append(path, entry)


def load_memory(path=MEMORY_PATH):
    return [row for row in _load(path) if row.get("id") and row.get("text")]


def read_memory(key=None, *, path=MEMORY_PATH, preview=120):
    entries = load_memory(path)
    if not entries:
        return "memory.jsonl 为空：先用 memory 工具写入一条经验。"
    if not key:
        lines = [
            f"- {e['id']} [{e.get('kind', 'note')}] {e['text'][:preview]}" for e in entries[-50:]
        ]
        return "记忆条目（最新在前请用 id 读取全文）:\n" + "\n".join(reversed(lines))
    needle = str(key).strip().lower()
    matches = [e for e in entries if e["id"].lower().startswith(needle)]
    if not matches:
        matches = [e for e in entries if needle in e["text"].lower()]
    if not matches:
        return f"无此记忆: {key}"
    return "\n\n".join(f"[{e['id']}] {e['text']}" for e in matches[-5:])


def memory_tool(arguments):
    """Dispatch entry used by agent.py: action=write|read|list."""
    if not isinstance(arguments, dict):
        return {"error": "memory 参数必须是对象"}
    action = str(arguments.get("action") or "read").lower()
    if action == "write":
        return write_memory(arguments.get("content") or "", kind=arguments.get("kind") or "note")
    if action in ("read", "list"):
        return read_memory(arguments.get("key"))
    return {"error": "unknown memory action; use write / read / list"}
