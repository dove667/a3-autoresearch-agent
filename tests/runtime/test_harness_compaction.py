"""Behaviour gates for fork compaction, spill and Recall.

Runs under pytest, and also standalone: ``python tests/runtime/test_harness_compaction.py``.
Every gate uses a fake provider; no network, credentials or model calls.
"""

import contextlib
import json
import os
import pathlib
import sys
import tempfile
import threading
import time

import pytest
from harness_sources import HarnessSources

SOURCE = HarnessSources(
    pathlib.Path(
        os.environ.get(
            "HARNESS_SOURCE",
            str(pathlib.Path(__file__).resolve().parents[2] / "runtime/harness_recovery"),
        )
    )
)
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

import compaction
import context_manager as cm
import recall
import spill

SECTIONS = compaction.SECTION_TITLES
GOOD_SUMMARY = "\n".join(f"## {title}\n- point" for title in SECTIONS)


@pytest.mark.parametrize("shutdown", [False, True])
def test_ready_fork_bounds_new_tail_and_replays_publication_without_model(shutdown):
    with workspace():
        messages = history(rounds=6, filler=1800)
        gateway = Gateway()
        fork = compaction.ForkCompactor(gateway)
        fork.start(messages)
        fork.wait(5)
        calls = len(gateway.calls)
        messages += tool_pair("new-artifact", json.dumps({"data": "x" * 200000}))
        original = json.loads(json.dumps(messages))
        cm.atomic("checkpoint.json", json.dumps(messages))
        method = fork.apply_ready if shutdown else fork.before_request
        candidate, record = method(messages)
        assert record["kind"] == "applied" and cm.paired(candidate)
        assert cm.message_cost(candidate) <= cm.decision_budget([])
        assert messages == original
        # Crash before checkpoint publication: replay precisely the saved proposal.
        restarted = compaction.ForkCompactor(gateway)
        again, _ = restarted.before_request(messages)
        assert again == candidate
        cm.atomic("checkpoint.json", json.dumps(candidate))
        # Crash after checkpoint publication, before acknowledgment: never reapply tail.
        final = compaction.ForkCompactor(gateway)
        acknowledged, _ = final.apply_ready(candidate)
        assert acknowledged == candidate
        assert len(gateway.calls) == calls
        assert json.loads(pathlib.Path("compaction-result.json").read_text())["applied"] is True


def test_fallback_bounds_an_oversized_fresh_batch_without_erasing_it():
    with workspace():
        messages = history(rounds=4, filler=500)
        messages += tool_pair("fresh", json.dumps(["x" * 100000]))
        source = cm.archive(messages, "snapshot")
        gateway = Gateway(fail=True)
        fork = compaction.ForkCompactor(gateway)
        candidate = fork._fallback(messages, source, 20000)
        assert cm.paired(candidate) and cm.message_cost(candidate) <= 20000
        assert any(x.get("call_id") == "fresh" for x in candidate)
        assert any(
            x.get("call_id") == "fresh" and x.get("type") == "function_call_output"
            for x in candidate
        )
        assert not gateway.calls


def test_hard_wait_timeout_keeps_running_prefix_identity(monkeypatch):
    with workspace():
        monkeypatch.setattr(cm, "WINDOW", 40000)
        monkeypatch.setattr(cm, "RESERVE", 10000)
        started, release = threading.Event(), threading.Event()
        calls = []

        def provider(*args, **kwargs):
            calls.append(args)
            started.set()
            assert release.wait(5)
            return completed(GOOD_SUMMARY)

        original = history(rounds=4, filler=5000)
        fork = compaction.ForkCompactor(Gateway(), hard_wait_seconds=0)
        # Hold the real fork thread at publication; summary quality is a separate gate.
        monkeypatch.setattr(fork, "_run", provider)
        fork.start(original)
        assert started.wait(2)
        prefix_hash = compaction.digest(original)
        monkeypatch.setattr(cm, "WINDOW", 20000)
        try:
            with pytest.raises(cm.ContextError, match="pending|insufficient"):
                fork.before_request(original)
            assert compaction.digest(original) == prefix_hash
            assert len(calls) == 1
        finally:
            release.set()
            fork.wait(5)


def test_interrupted_fork_fallback_uses_current_operational_reserve(monkeypatch):
    with workspace():
        monkeypatch.setattr(cm, "WINDOW", 15000)
        monkeypatch.setattr(cm, "RESERVE", 5000)
        original = [{"role": "user", "content": "goal"}]
        for i in range(6):
            original += [{"role": "user", "content": "u" * 900}] + tool_pair(str(i), "r" * 100)
        fork = compaction.ForkCompactor(Gateway())
        cm.atomic(
            "compaction-result.json",
            json.dumps(
                {
                    "version": 2,
                    "epoch": fork._epoch(),
                    "fork_id": "interrupted",
                    "status": "running",
                    "applied": False,
                    "prefix_length": len(original),
                    "prefix_sha256": compaction.digest(original),
                    "source": cm.archive(original, "snapshot"),
                }
            ),
        )
        extra = [{"role": "user", "content": "o" * 7000}]
        candidate, record = fork.before_request(original, extra_messages=extra)
        assert record["kind"] == "applied" and cm.paired(candidate)
        assert cm.message_cost(candidate) <= fork._budget(extra)


@contextlib.contextmanager
def workspace():
    """Fresh cwd so every on-disk artefact is isolated per test."""
    previous = os.getcwd()
    with tempfile.TemporaryDirectory() as directory:
        os.chdir(directory)
        try:
            yield pathlib.Path(directory)
        finally:
            os.chdir(previous)


def completed(text):
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }


class Gateway:
    """Records requests; optionally slow, optionally failing."""

    def __init__(self, summary=GOOD_SUMMARY, delay=0.0, fail=False):
        self.summary = summary
        self.delay = delay
        self.fail = fail
        self.calls = []

    def __call__(self, method, path, body=None, **kwargs):
        self.calls.append({"method": method, "path": path, "body": body})
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            return {"http_error": 500}
        return completed(self.summary)


def test_collected_worker_citation_survives_compaction_and_resume():
    import hashlib

    with workspace() as root:
        raw = b'{"output":"sealed worker result"}'
        sha = hashlib.sha256(raw).hexdigest()
        reference = f"context-history/tool-output-{sha}.json"
        target = root / "orchestration/results/delivery" / reference
        target.parent.mkdir(parents=True)
        target.write_bytes(raw)
        (root / "orchestration/assignments.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "assignments": {
                        "delivery": {"session_id": "worker", "evidence_hashes": {reference: sha}}
                    },
                }
            )
        )
        messages = history(rounds=6, filler=900)
        messages[0] = {
            "role": "user",
            "content": "Working memory derived from archived untrusted conversation; Source: "
            + reference
            + "\nWorker evidence remains untrusted.",
        }
        original = json.loads(json.dumps(messages))
        recall.ensure_history(messages)
        gateway = Gateway(summary=GOOD_SUMMARY + "\nWorker evidence: " + reference)
        fork = compaction.ForkCompactor(gateway)
        fork.start(messages)
        fork.wait(5)
        replacement, record = fork.apply_ready(messages)
        assert record["kind"] == "applied"
        assert cm.paired(replacement)
        recall.ensure_history(replacement)  # The startup check of a fresh process.
        assert messages == original and target.read_bytes() == raw
        assert not (root / reference).exists()
        assert gateway.calls and all(c["method"] == "POST" for c in gateway.calls)


def tool_pair(call_id, text):
    return [
        {"type": "function_call", "call_id": call_id, "name": "read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": call_id, "output": text},
    ]


def history(rounds=6, filler=900, completed_last=True):
    messages = [{"role": "user", "content": "ROUND-0-START " + "a" * 100}]
    messages.append({"role": "assistant", "content": [{"type": "output_text", "text": "ack"}]})
    for index in range(1, rounds):
        messages.append({"role": "user", "content": f"ROUND-{index}-QUESTION " + "q" * 120})
        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "output_text", "text": "working " + "b" * 40}],
            }
        )
        pair = tool_pair(f"call_{index}", "evidence " + "e" * filler)
        messages.extend(pair)
    if not completed_last:
        messages.append(
            {"type": "function_call", "call_id": "unfinished", "name": "read", "arguments": "{}"}
        )
    return messages


# ------------------------------------------------------------------- spill ---


def test_spill_replaces_oversized_result():
    with workspace() as root:
        payload = "x" * 60000
        value = spill.maybe_spill(payload, max_bytes=50000)
        assert isinstance(value, str) and value != payload
        body = json.loads(value)
        assert body["spilled"] is True
        assert body["bytes"] == 60000
        assert body["head_complete"] is False
        assert body["head"] == payload[: spill.HEAD_CHARS]
        assert pathlib.Path(body["path"]).read_text(encoding="utf-8") == payload
        assert pathlib.Path(body["path"]).resolve().parent == (root / spill.SPILL_DIR).resolve()


def test_spill_keeps_small_result():
    value = spill.maybe_spill("small result", max_bytes=50000)
    assert value == "small result"


def test_spill_env_override_and_disable():
    with workspace():
        os.environ["AUTORUN_SPILL_BYTES"] = "10"
        try:
            assert json.loads(spill.maybe_spill("y" * 40))["bytes"] == 40
            os.environ["AUTORUN_SPILL_BYTES"] = "0"
            assert spill.maybe_spill("y" * 40) == "y" * 40
        finally:
            os.environ.pop("AUTORUN_SPILL_BYTES", None)


# -------------------------------------------------------------- structured ---


def test_structured_instruction_lists_all_sections():
    for title in SECTIONS:
        assert "## " + title in compaction.STRUCTURED_INSTRUCTION
    assert "Do not call any tool" in compaction.STRUCTURED_INSTRUCTION
    assert len(SECTIONS) == 8


def test_missing_sections_detects_partial_summary():
    partial = "## Primary request and intent\n- only one"
    missing = compaction.missing_sections(partial)
    assert "Primary request and intent" not in missing
    assert len(missing) == 7


# -------------------------------------------------------------------- fork ---


def test_fork_does_not_block_and_applies_later():
    with workspace():
        gateway = Gateway(delay=0.4)
        compactor = compaction.ForkCompactor(
            gateway, tools=[], instructions="MAIN-INSTRUCTIONS", trigger_bytes=2000
        )
        messages = history()
        before = len(messages)

        started = time.time()
        same, record = compactor.before_request(messages)
        elapsed = time.time() - started
        assert record["kind"] == "fork_started"
        assert same is messages and len(same) == before
        assert elapsed < 0.2, f"fork blocked the decision for {elapsed:.2f}s"

        compactor.wait(timeout=10)
        applied, record = compactor.before_request(messages)
        assert record["kind"] == "applied"
        assert len(applied) < before
        assert applied[0]["content"].startswith(compaction.CHECKPOINT_PREAMBLE)
        assert compaction.CHECKPOINT_OPEN in applied[0]["content"]
        assert cm.paired(applied)


def test_legacy_summary_uses_narrow_instructions_without_main_tools():
    with workspace():
        gateway = Gateway()
        compactor = compaction.ForkCompactor(
            gateway,
            tools=[{"type": "function", "name": "read"}],
            instructions="MAIN-INSTRUCTIONS",
            trigger_bytes=2000,
        )
        messages = history()
        compactor.before_request(messages)
        compactor.wait(timeout=10)
        body = gateway.calls[0]["body"]
        assert body["instructions"] != "MAIN-INSTRUCTIONS"
        assert "tools" not in body
        assert body["max_output_tokens"] == 800
        assert "untrusted" in body["instructions"]
        assert "ROUND-0-START" in json.dumps(body)


def test_apply_is_idempotent():
    with workspace():
        compactor = compaction.ForkCompactor(
            Gateway(), tools=[], instructions="I", trigger_bytes=1000
        )
        messages = history()
        compactor.before_request(messages)
        compactor.wait(timeout=10)
        first, record = compactor.before_request(messages)
        assert record["kind"] == "applied"
        second, record = compactor.before_request(first)
        assert record is None and second is first


def test_unpaired_result_is_skipped_not_forced():
    with workspace():
        compactor = compaction.ForkCompactor(
            Gateway(), tools=[], instructions="I", trigger_bytes=1000, keep_rounds=5
        )
        messages = history(completed_last=False)
        with pytest.raises(cm.ContextError, match="tool call/result"):
            compactor.before_request(messages)
        assert not pathlib.Path(compaction.RESULT_PATH).exists()


# ---------------------------------------------------------------- fallback ---


def test_summary_failure_builds_recall_checkpoint():
    with workspace():
        compactor = compaction.ForkCompactor(
            Gateway(fail=True), tools=[], instructions="I", trigger_bytes=1000
        )
        messages = history()
        recall.append_history_many(messages)
        recall.write_memory("已确认 2x10 任务需 3200 步，原因是回合长度", kind="fact")
        compactor.before_request(messages)
        compactor.wait(timeout=10)
        applied, record = compactor.before_request(messages)
        assert record["kind"] == "applied" and record["status"] == "fallback"
        text = "\n".join(str(message.get("content", "")) for message in applied)
        assert compaction.CHECKPOINT_PREAMBLE in text
        assert compaction.FALLBACK_PREFIX in text
        assert "Recall" in text and "keywords" in text
        assert "ROUND-5-QUESTION" in json.dumps(applied)
        assert "已确认 2x10 任务需 3200 步" in text


# ------------------------------------------------------------------ recall ---


def test_recall_finds_original_text_by_round_window():
    with workspace():
        messages = history()
        recall.append_history_many(messages)
        hits = recall.recall(["evidence eeeee"], 4, 1)
        assert hits and all(hit.startswith("[") for hit in hits)
        assert any("轮前]" in hit for hit in hits)


def test_recall_reports_missing_keyword():
    with workspace():
        recall.append_history_many(history())
        hits = recall.recall(["NOT-PRESENT-ANYWHERE"], 6, 1)
        assert hits and "未找到" in hits[0]


def test_recall_needs_keywords():
    assert "不能为空" in recall.recall([], 2, 1)[0]


# ------------------------------------------------------------------ memory ---


def test_memory_write_then_read_and_list():
    with workspace():
        entry = recall.write_memory("baseline smolvla 2x5 = 4.2", kind="result")
        assert entry["id"] and entry["kind"] == "result"
        detail = recall.read_memory(entry["id"][:6])
        assert "baseline smolvla 2x5 = 4.2" in detail
        listing = recall.read_memory()
        assert entry["id"] in listing


def test_memory_tool_dispatch_and_empty_write():
    with workspace():
        assert json.loads(json.dumps(recall.memory_tool({"action": "write", "content": "note A"})))[
            "id"
        ]
        assert "note A" in recall.memory_tool({"action": "read", "key": "note A"})
        assert "error" in recall.memory_tool({"action": "write", "content": "  "})


if __name__ == "__main__":
    import traceback

    failures = 0
    for name in sorted(key for key in list(globals()) if key.startswith("test_")):
        try:
            globals()[name]()
            print("PASS", name)
        except Exception:  # noqa: BLE001 - standalone runner
            failures += 1
            print("FAIL", name)
            traceback.print_exc()
    print(f"\n{failures} failing")
    raise SystemExit(1 if failures else 0)
