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
import pathlib
import time

HISTORY_PATH = "history.jsonl"
MEMORY_PATH = "memory.jsonl"
SNIPPET_WINDOW = 100
SNIPPET_LIMIT = 30


def _append(path, payload):
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
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
    """Split on user turns: one round runs from a user message to the next one."""
    rounds, current = [], []
    for message in messages:
        if not isinstance(message, dict):
            continue
        is_user_turn = message.get("role") == "user" and message.get("type") != "function_call_output"
        if is_user_turn and current:
            rounds.append(current)
            current = [message]
        else:
            current.append(message)
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


def recall(keywords, n, m, *, messages=None, path=HISTORY_PATH,
           window=SNIPPET_WINDOW, limit=SNIPPET_LIMIT):
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
            f"- {e['id']} [{e.get('kind', 'note')}] {e['text'][:preview]}"
            for e in entries[-50:]
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
