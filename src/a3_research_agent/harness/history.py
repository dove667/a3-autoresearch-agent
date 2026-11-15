"""Preserve original conversation history and retrieve its source evidence."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from collections import Counter

from a3_research_agent.harness import jsonl_store

HISTORY_PATH = "history.jsonl"
SNIPPET_WINDOW = 100
SNIPPET_LIMIT = 30


def append_history(message, path=HISTORY_PATH):
    """Persist one model-visible message verbatim, forever."""
    if not isinstance(message, dict):
        return None
    return jsonl_store.append_record(path, message)


def append_history_many(messages, path=HISTORY_PATH):
    written = 0
    for message in messages or []:
        if append_history(message, path) is not None:
            written += 1
    return written


def load_history(path=HISTORY_PATH):
    return [row for row in jsonl_store.load_records(path) if row.get("type") != "corrupt_line"]


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


def _verify_archive_reference(reference):
    path = pathlib.Path(reference)
    expected = path.stem.rsplit("-", 1)[-1]
    return _archive_matches(path, expected)


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
                raise ValueError("Unsupported checkpoint without a complete summary")
            recognized = line.startswith(
                "Working memory derived from archived untrusted conversation;"
            )
            if content.startswith("Untrusted derived working memory,") and not recognized:
                raise ValueError("Invalid framed control archive source header")
            if recognized and source is None:
                raise ValueError("Missing control archive source header")
            if source:
                reference = source.group(1)
                if not re.fullmatch(
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
    for reference in references:
        if not _verify_archive_reference(reference):
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

    with jsonl_store.LOCK:
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


def iter_rounds(messages):
    """Split on completed decision batches as well as explicit user turns.

    Autonomous runs often have one initial user turn followed by many tool
    batches. Parallel calls and their outputs remain in the same round.
    """
    current, pending = [], set()
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
            yield current
            current, completed_tools = [], False
        current.append(message)
        if kind == "function_call":
            pending.add(message.get("call_id"))
        elif kind == "function_call_output":
            pending.discard(message.get("call_id"))
            completed_tools = not pending
    if current:
        yield current


def split_rounds(messages):
    return list(iter_rounds(messages))


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
    n=None,
    m=None,
    *,
    messages=None,
    path=HISTORY_PATH,
    window=SNIPPET_WINDOW,
    limit=SNIPPET_LIMIT,
    mode="evidence",
):
    """Search available original history, optionally restricted to relative rounds."""
    terms = list(
        dict.fromkeys(term[:160] for key in (keywords or []) for term in str(key).split())
    )[:12]
    if not terms:
        return ["keywords 不能为空：传入 1 个或多个关键字"]
    try:
        half = max(1, min(400, int(window)) // 2)
        limit = max(1, min(SNIPPET_LIMIT, int(limit)))
        bounds = [None if value is None else max(1, int(value)) for value in (n, m)]
    except (TypeError, ValueError):
        return ["n、m、window、limit 必须为整数：1 表示最近一轮"]
    if mode not in ("evidence", "raw"):
        return ["mode 必须为 evidence 或 raw"]

    def source():
        if messages is not None:
            yield from messages
            return
        target = pathlib.Path(path)
        if target.is_file():
            with target.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        yield value

    # Two bounded passes retain a single round and ranked snippets, rather than
    # loading the lifetime transcript and every parsed tool result into memory.
    total_rounds = sum(1 for _ in iter_rounds(source()))
    if not total_rounds:
        return ["history.jsonl 中没有可用历史（尚无记录或被清空）"]
    n_int, m_int = sorted(
        (bounds[0] if bounds[0] is not None else total_rounds, bounds[1] or 1), reverse=True
    )
    pattern = re.compile("|".join(map(re.escape, terms)), re.IGNORECASE)
    candidates = []
    for index, group in enumerate(iter_rounds(source())):
        ago = total_rounds - index
        if not m_int <= ago <= n_int:
            continue
        call_names = {
            message.get("call_id"): message.get("name")
            for message in group
            if message.get("type") == "function_call"
        }
        for message in group:
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
            is_output = message.get("type") == "function_call_output"
            candidate = (len(matched_terms), is_output, -ago, snippet)
            previous = next((hit for hit in candidates if hit[3] == snippet), None)
            if previous is not None:
                if previous[:3] >= candidate[:3]:
                    continue
                candidates.remove(previous)
            candidates.append(candidate)
            candidates.sort(key=lambda hit: hit[:3], reverse=True)
            del candidates[limit:]
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
        arguments.get("n"),
        arguments.get("m"),
        window=arguments.get("window", SNIPPET_WINDOW),
        limit=arguments.get("limit", SNIPPET_LIMIT),
        mode=arguments.get("mode", "evidence"),
    )
