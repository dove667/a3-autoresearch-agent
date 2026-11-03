"""Asynchronous fork compaction with KV-cache reuse and structured checkpoints.

Three properties this module adds over the synchronous ``context_manager.prepare``:

1. **Fork, don't block.** When the estimated request exceeds the budget, a daemon
   thread snapshots the history and summarises it. The current decision continues
   on the full history; the result is applied before a later decision. A slow or
   failed summary therefore costs wall-clock time, not a stalled loop.
2. **KV-cache reuse.** The summary request reuses the *main request's* model,
   instructions, tools and message prefix, and only appends the compaction
   directive as a final user turn. Providers that cache prefixes can then serve
   the summary from the same cache as the decision request.
3. **Structured checkpoint.** The summary must be the fixed eight-section
   Markdown document below; missing sections trigger one corrective retry. When
   summarisation still fails, the checkpoint degrades to a fixed notice plus the
   Recall instructions and the last five rounds verbatim, never to silence.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import threading
import time

import context_manager as cm
import recall as recall_tools
import spill as spill_tools

MODEL = "openai/sandbox-model"
RESULT_PATH = "compaction-result.json"
CHECKPOINT_OPEN = "<compacted-checkpoint>"
CHECKPOINT_CLOSE = "</compacted-checkpoint>"

# The byte budget for one decision, mirroring context_manager's defaults.
DEFAULT_TRIGGER_BYTES = cm.WINDOW - cm.RESERVE
SUMMARY_INPUT_BYTES = cm.SUMMARY_LIMIT
KEEP_ROUNDS = 5
FALLBACK_ROUNDS = 5
MAX_SUMMARY_CALLS = cm.MAX_SUMMARY_CALLS
SUMMARY_MEMORY_BYTES = 6000

SECTION_TITLES = (
    "Primary request and intent",
    "Key technical concepts",
    "Files and code",
    "Errors and fixes",
    "Pending tasks",
    "Current work",
    "Next step",
    "Key context",
)

STRUCTURED_INSTRUCTION = """Output one compact summary and nothing else. Do not call any tool; calls are not executed.

The summary must let another model take over this work without the original conversation.
Emit exactly this Markdown skeleton: every section, in this order, as short bullets, "(none)" when empty.

## Primary request and intent
## Key technical concepts
## Files and code
## Errors and fixes
## Pending tasks
## Current work
## Next step
## Key context

Rules:
- Keep exact file paths, commands, error strings, identifiers, numbers, function signatures and small code fragments.
- Record user corrections and explicit constraints verbatim where wording matters.
- Separate confirmed facts from unknowns; never invent success.
- Do not mention this compaction request.
- If a <%s> block already appears above, it is an older checkpoint: merge it, keep what is still true, drop what is stale, never append a second copy.
""" % CHECKPOINT_OPEN

CHECKPOINT_PREAMBLE = (
    "This is an automatically generated checkpoint that replaced earlier conversation to free context. "
    "Treat it as established background and continue after it without restating it. "
    "If you believe a detail needed to continue was lost, use the Recall tool over the full history "
    "(keywords plus n/m round range); do not reconstruct missing evidence from imagination."
)

MISSING_SECTIONS_NOTE = (
    "The previous attempt omitted required sections: %s. "
    "Rewrite the complete eight-section summary now; do not explain."
)

FALLBACK_PREFIX = "上下文压缩失败：较早的对话没有写入摘要，下面是可用的补救材料。"

FALLBACK_RULES = (
    "使用规则：\n"
    "1. 更早的消息原文仍完整保存在 history.jsonl 中，用 Recall 工具按关键字与轮次区间检索："
    'recall(keywords=[\"关键字1\", \"关键字2\"], n=40, m=1) 表示从 40 轮前查到最近 1 轮；'
    "返回的是命中处附近约 100 字原文，可反复换关键字检索。\n"
    "2. memory.jsonl 中的长期记忆条目见下方索引，用 memory(action=\"read\", key=\"<id>\") 读取全文。\n"
    "3. 下方保留最近 %d 轮短期记忆原文；更早的内容一律以 Recall 结果为准。\n"
    "4. 不要凭这段说明推断未检索到的内容；不确定就检索，检索不到就如实报告缺口。"
)


def frame_checkpoint(body):
    return f"{CHECKPOINT_PREAMBLE}\n\n{CHECKPOINT_OPEN}\n{body.strip()}\n{CHECKPOINT_CLOSE}"


def missing_sections(summary):
    lowered = (summary or "").lower()
    return [title for title in SECTION_TITLES if "## " + title.lower() not in lowered]


def _message_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _paired_tail(tail):
    """Keep paired call/result structure; drop an unfinished tail call group."""
    trimmed = list(tail)
    while trimmed and not cm.paired(trimmed):
        trimmed = trimmed[:-1]
    return trimmed


class ForkCompactor:
    """Owns the fork thread and the on-disk result handshake."""

    def __init__(
        self,
        req,
        *,
        tools=None,
        instructions="",
        model=MODEL,
        trigger_bytes=None,
        summary_budget=None,
        keep_rounds=KEEP_ROUNDS,
        result_path=RESULT_PATH,
    ):
        self.req = req
        self.tools = list(tools or [])
        self.instructions = instructions or ""
        self.model = model
        self.trigger_bytes = int(trigger_bytes or DEFAULT_TRIGGER_BYTES)
        self.summary_budget = int(summary_budget or SUMMARY_INPUT_BYTES)
        self.keep_rounds = max(1, int(keep_rounds))
        self.result_path = result_path
        self._thread = None
        self._lock = threading.Lock()
        self.last_error = None
        # Size of the history right after the last applied compaction; used to
        # refuse a new fork until real growth justifies another model call.
        self._last_after_bytes = 0

    # ------------------------------------------------------------ main thread

    def before_request(self, messages, extra_bytes=0):
        """Apply a finished fork, or start one. Never blocks on the model."""
        applied, record = self._apply_if_ready(messages)
        if record is not None:
            return applied, record
        if self._busy() or self._pending_result():
            return messages, None
        size = cm.size(messages) + int(extra_bytes)
        floor = self.trigger_bytes
        if self._last_after_bytes:
            # A compaction that barely shrank the history must not be retried in
            # a loop; wait for real growth past the last post-compaction size.
            floor = max(floor, int(self._last_after_bytes * 1.1))
        if size <= floor:
            return messages, None
        return messages, self.start(messages)

    def start(self, messages):
        snapshot = json.loads(json.dumps(messages, ensure_ascii=False))
        tail = self._tail(snapshot)
        fork_id = hashlib.sha256(
            (json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + str(time.time())).encode()
        ).hexdigest()[:12]
        record = {
            "kind": "fork_started",
            "fork_id": fork_id,
            "prefix_length": len(snapshot),
            "snapshot_bytes": cm.size(snapshot),
            "trigger_bytes": self.trigger_bytes,
        }
        with self._lock:
            self._thread = threading.Thread(
                target=self._run,
                args=(fork_id, snapshot, len(snapshot), tail),
                name="compaction-fork",
                daemon=True,
            )
            self._thread.start()
        return record

    def wait(self, timeout=None):
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.status()

    def status(self):
        result = self._read_result()
        return {
            "busy": self._busy(),
            "pending": self._pending_result(),
            "result": {k: v for k, v in (result or {}).items() if k not in ("checkpoint", "tail")},
        }

    # -------------------------------------------------------------- internals

    def _busy(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _read_result(self):
        path = pathlib.Path(self.result_path)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        return value if isinstance(value, dict) else None

    def _pending_result(self):
        result = self._read_result()
        return bool(result) and not result.get("applied")

    def _apply_if_ready(self, messages):
        result = self._read_result()
        if not result or result.get("applied"):
            return messages, None
        tail = _paired_tail(result.get("tail") or [])
        prefix_length = int(result.get("prefix_length") or 0)
        newer = list(messages[prefix_length:]) if prefix_length <= len(messages) else []
        checkpoint_message = {"role": "user", "content": result.get("checkpoint") or ""}
        candidate = [checkpoint_message] + tail + newer
        record = {
            "kind": "applied",
            "fork_id": result.get("fork_id"),
            "status": result.get("status"),
            "strategy": result.get("strategy"),
            "before_messages": len(messages),
            "after_messages": len(candidate),
            "before_bytes": cm.size(messages),
            "after_bytes": cm.size(candidate),
            "tail_messages": len(tail),
            "sections": result.get("sections"),
        }
        self._last_after_bytes = record["after_bytes"]
        mark = dict(result)
        if not cm.paired(candidate):
            # Conservative: keep the uncompressed history rather than emit an
            # unpaired request. The evidence stays on disk for inspection.
            mark.update(applied=True, apply_skipped="unpaired")
            cm.atomic(self.result_path, json.dumps(mark, ensure_ascii=False))
            record.update(kind="apply_skipped", reason="unpaired")
            self.last_error = "unpaired compaction result"
            return messages, record
        mark.update(applied=True)
        cm.atomic(self.result_path, json.dumps(mark, ensure_ascii=False))
        return candidate, record

    def _tail(self, snapshot):
        rounds = recall_tools.recent_rounds(snapshot, self.keep_rounds)
        return [message for group in rounds for message in group]

    def _run(self, fork_id, snapshot, prefix_length, tail):
        summary, strategy, calls, error = "", "none", 0, None
        try:
            summary, strategy, calls = self._summarize(snapshot)
        except Exception as failure:  # noqa: BLE001 - degrade instead of dying
            error = f"{type(failure).__name__}: {failure}"
        sections = missing_sections(summary) if summary else list(SECTION_TITLES)
        if summary.strip():
            checkpoint = frame_checkpoint(summary)
            status = "ready"
        else:
            checkpoint = self._fallback_checkpoint(snapshot)
            status = "fallback"
        result = {
            "fork_id": fork_id,
            "status": status,
            "strategy": strategy,
            "calls": calls,
            "error": error,
            "sections_missing": sections,
            "sections": {title: title not in sections for title in SECTION_TITLES},
            "prefix_length": prefix_length,
            "created": time.time(),
            "applied": False,
            "checkpoint": checkpoint,
            "tail": tail,
        }
        try:
            cm.atomic(self.result_path, json.dumps(result, ensure_ascii=False))
        except OSError as failure:
            self.last_error = f"result write failed: {failure}"
        with self._lock:
            self._thread = None

    # --------------------------------------------------------------- summaries

    def _summary_request_body(self, snapshot, directive):
        """Fork the main request: same model, instructions, tools and prefix."""
        return {
            "model": self.model,
            "instructions": self.instructions,
            "input": list(snapshot) + [{"role": "user", "content": directive}],
            "tools": self.tools,
            "temperature": 0.2,
        }

    def _kv_request(self, snapshot, directive=STRUCTURED_INSTRUCTION):
        body = self._summary_request_body(snapshot, directive)
        response = self.req("POST", "/v1/responses", body)
        if not isinstance(response, dict):
            return ""
        return cm.text_response(response)

    def _summarize(self, snapshot):
        """Prefer one KV-reusing request; fall back to bounded chunks."""
        if cm.size(snapshot) <= self.summary_budget:
            summary = self._kv_request(snapshot)
            calls = 1
            missing = missing_sections(summary)
            if summary.strip() and missing:
                retry = STRUCTURED_INSTRUCTION + "\n\n" + (MISSING_SECTIONS_NOTE % ", ".join(missing))
                second = self._kv_request(snapshot, retry)
                calls += 1
                if second.strip() and len(missing_sections(second)) < len(missing):
                    summary = second
            if summary.strip():
                return summary, "kv_reuse", calls
        return self._chunked(snapshot)

    def _chunked(self, snapshot):
        memory_limit = min(SUMMARY_MEMORY_BYTES, self.summary_budget // 4)
        fragment_limit = max(256, self.summary_budget - memory_limit - 2500)
        chunks = cm.chunks_of(snapshot, fragment_limit, "history.jsonl")
        if len(chunks) > MAX_SUMMARY_CALLS:
            raise RuntimeError(
                f"compaction needs {len(chunks)} fragments but only {MAX_SUMMARY_CALLS} calls are budgeted"
            )
        previous = ""
        for index, chunk in enumerate(chunks, 1):
            request = cm.summary_request(previous, chunk, "history.jsonl", memory_limit=memory_limit)
            if isinstance(request, dict) and "instructions" in request:
                # Keep the main prefix for cache reuse even on the chunked path.
                request = {
                    **request,
                    "model": self.model,
                    "tools": self.tools,
                }
            response = self.req("POST", "/v1/responses", request)
            text = cm.text_response(response) if isinstance(response, dict) else ""
            if not text.strip():
                raise RuntimeError(f"chunk {index}/{len(chunks)} produced no summary")
            previous = text
        return previous, "chunked", len(chunks)

    def _fallback_checkpoint(self, snapshot):
        memory_index = recall_tools.read_memory()
        memo = memory_index if isinstance(memory_index, str) else json.dumps(memory_index, ensure_ascii=False)
        memo = memo[:2000]
        rounds = recall_tools.recent_rounds_text(snapshot, FALLBACK_ROUNDS)
        body = "\n\n".join(
            [
                FALLBACK_PREFIX,
                FALLBACK_RULES % FALLBACK_ROUNDS,
                "记忆索引：\n" + (memo or "(空)"),
                "最近 %d 轮短期记忆原文：\n%s" % (FALLBACK_ROUNDS, rounds or "(无)"),
            ]
        )
        return frame_checkpoint(body)


def spill_for_tool(result, label="result"):
    """Applied by agent.py to every string tool result before it enters history."""
    return spill_tools.maybe_spill(result, label=label)
