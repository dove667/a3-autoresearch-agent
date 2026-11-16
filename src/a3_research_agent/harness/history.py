"""Preserve original conversation history and retrieve its source evidence."""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
import re
from collections import Counter

from a3_research_agent.harness import jsonl_store, state_paths

HISTORY_PATH = state_paths.HISTORY


def append_history(message, path=None):
    """Persist one model-visible message verbatim, forever."""
    if not isinstance(message, dict):
        return None
    return jsonl_store.append_record(
        state_paths.path(HISTORY_PATH) if path is None else path, message
    )


def append_history_many(messages, path=None):
    written = 0
    for message in messages or []:
        if append_history(message, path) is not None:
            written += 1
    return written


def load_history(path=None):
    return [
        row
        for row in jsonl_store.load_records(
            state_paths.path(HISTORY_PATH) if path is None else path
        )
        if row.get("type") != "corrupt_line"
    ]


def _archive_matches(path, expected):
    """Verify logical sources in the state store without following archive symlinks."""
    if path.is_absolute():
        return False
    current = state_paths.state_root()
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
    for reference in references:
        if not _verify_archive_reference(reference):
            raise ValueError(
                "Referenced original archive is missing or corrupt; explicit recovery required"
            )
    return len(references)


def ensure_history(messages, path=None):
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
        target = state_paths.path(HISTORY_PATH) if path is None else pathlib.Path(path)
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


RETRIEVAL_TOOLS = frozenset({"recall", "memory", "plan", "research_status"})


def recall_tool(arguments):
    """Validate the small model-facing contract before searching current-run history."""
    if not isinstance(arguments, dict) or arguments.keys() - {
        "keywords",
        "recent_rounds",
        "limit",
        "mode",
    }:
        return {
            "error": "invalid_recall",
            "hint": "Use keywords, optional recent_rounds, limit and mode.",
        }
    keywords = arguments.get("keywords")
    if (
        not isinstance(keywords, list)
        or not 1 <= len(keywords) <= 12
        or any(not isinstance(term, str) or not term.strip() for term in keywords)
    ):
        return {
            "error": "invalid_keywords",
            "hint": "Supply 1–12 nonempty search terms for this run's original history.",
        }
    rounds, limit, mode = (
        arguments.get("recent_rounds"),
        arguments.get("limit", 6),
        arguments.get("mode", "evidence"),
    )
    if (
        (rounds is not None and (type(rounds) is not int or rounds < 1))
        or type(limit) is not int
        or not 1 <= limit <= 12
        or mode not in {"evidence", "raw"}
    ):
        return {
            "error": "invalid_recall",
            "hint": "Use positive recent_rounds, limit within1–12, mode evidence or raw.",
        }
    return search_current_history(keywords, recent_rounds=rounds, limit=limit, mode=mode)


def search_current_history(keywords, *, recent_rounds=None, limit=6, mode="evidence"):
    """Return source-linked observations, including verified large-output originals."""
    from a3_research_agent.harness.context import source_search

    terms = list(dict.fromkeys(term[:160] for keyword in keywords for term in keyword.split()))[:12]
    if not terms:
        return {"error": "invalid_keywords", "hint": "Supply at least one searchable term."}
    pattern = re.compile("|".join(map(re.escape, terms)), re.IGNORECASE)
    result = {"scope": "current_run", "items": [], "warnings": []}
    history_path = state_paths.path(HISTORY_PATH)
    if not history_path.exists():
        return {**result, "notice": "No current-run history has been recorded."}
    cache, warnings, candidates = {}, set(), []

    def history_rows(stream):
        offset = 0
        for line in stream:
            try:
                message = json.loads(line)
            except ValueError:
                warnings.add((HISTORY_PATH, "invalid_history_record"))
                offset += len(line)
                continue
            if isinstance(message, dict):
                yield {**message, "_recall_offset": offset, "_recall_line": line}
            offset += len(line)

    def add_hit(hit, source, ago, tool_output):
        if hit is None:
            return
        item = {
            "rounds_ago": ago,
            "snippet": hit["snippet"],
            "matched_terms": hit["matched_terms"],
            "source": {
                **source,
                "offset": source.get("offset", 0) + hit["offset"],
                "limit": min(8000, max(200, len(hit["snippet"]))),
            },
        }
        score = (len(hit["matched_terms"]), tool_output, -ago)
        previous = next(
            (entry for entry in candidates if entry[1]["snippet"] == item["snippet"]), None
        )
        if previous is not None:
            if previous[0] >= score:
                return
            candidates.remove(previous)
        candidates.append((score, item))
        candidates.sort(key=lambda entry: entry[0], reverse=True)
        del candidates[limit:]

    try:
        with source_search.verified_text(HISTORY_PATH) as (stream, digest):
            total_rounds = sum(1 for _ in iter_rounds(history_rows(stream)))
            stream.seek(0)
            for index, group in enumerate(iter_rounds(history_rows(stream))):
                ago = total_rounds - index
                if recent_rounds is not None and ago > recent_rounds:
                    continue
                names = {
                    message.get("call_id"): message.get("name")
                    for message in group
                    if message.get("type") == "function_call"
                }
                for message in group:
                    name, kind = names.get(message.get("call_id")), message.get("type")
                    if name == "recall" or (mode == "evidence" and name in RETRIEVAL_TOOLS):
                        continue
                    if kind == "reasoning" or (mode == "evidence" and kind == "function_call"):
                        continue
                    # Serialized-source offsets can be passed directly to read.
                    add_hit(
                        source_search.find_text(io.StringIO(message["_recall_line"]), pattern),
                        {
                            "path": HISTORY_PATH,
                            "sha256": digest,
                            "offset": message["_recall_offset"],
                            "call_id": message.get("call_id"),
                        },
                        ago,
                        kind == "function_call_output",
                    )
                    if kind != "function_call_output":
                        continue
                    try:
                        value = json.loads(message.get("output", ""))
                    except (ValueError, TypeError):
                        continue
                    try:
                        sources = source_search.output_sources(value)
                    except ValueError as error:
                        warnings.add((str(value.get("path", ""))[:200], str(error)))
                        continue
                    for reference, expected in sources:
                        key = (reference, expected)
                        if key not in cache:
                            try:
                                with source_search.verified_text(reference, expected) as (
                                    original,
                                    _,
                                ):
                                    hit = source_search.find_text(original, pattern)
                                cache[key] = (hit, True)
                            except (OSError, ValueError, UnicodeError):
                                warnings.add((reference, "source_unavailable_or_unverified"))
                                cache[key] = (None, False)
                        hit, verified = cache[key]
                        if len(cache) > 256:
                            cache.pop(next(iter(cache)))
                        if not verified:
                            break
                        add_hit(
                            hit,
                            {
                                "path": reference,
                                "sha256": expected,
                                "call_id": message.get("call_id"),
                            },
                            ago,
                            True,
                        )
    except (OSError, ValueError, UnicodeError):
        return {
            "error": "history_unavailable_or_changed",
            "hint": "Retry current-run recall; preserve the history if the failure continues.",
        }
    result["items"] = [item for _, item in candidates]
    ordered_warnings = sorted(warnings)
    result["warnings"] = [{"path": path, "error": error} for path, error in ordered_warnings[:12]]
    result["omitted_warnings"] = max(0, len(ordered_warnings) - 12)
    if not result["items"]:
        result["notice"] = "No matching observation in the selected current-run rounds."
    return result
