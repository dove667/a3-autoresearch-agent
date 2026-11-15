"""Behaviour gates for fork compaction, spill and Recall.

Runs under pytest, and also standalone: ``python tests/runtime/test_harness_compaction.py``.
Every gate uses a fake provider; no network, credentials or model calls.
"""

import contextlib
import json
import os
import pathlib
import tempfile
import threading
import time

import pytest

from a3_research_agent.harness import history as recall
from a3_research_agent.harness import memory, paths, state_paths
from a3_research_agent.harness.context import compaction, spill
from a3_research_agent.harness.context import manager as cm

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
        cm.atomic(state_paths.path("checkpoint.json"), json.dumps(messages))
        method = fork.apply_ready if shutdown else fork.before_request
        candidate, record = method(messages)
        assert record["kind"] == "applied" and cm.paired(candidate)
        assert cm.message_cost(candidate) <= cm.decision_budget([])
        assert messages == original
        restarted = compaction.ForkCompactor(gateway)
        again, _ = restarted.before_request(messages)
        assert again == candidate
        cm.atomic(state_paths.path("checkpoint.json"), json.dumps(candidate))
        final = compaction.ForkCompactor(gateway)
        acknowledged, _ = final.apply_ready(candidate)
        assert acknowledged == candidate
        assert len(gateway.calls) == calls
        assert json.loads(state_paths.path("compaction-result.json").read_text())["applied"] is True


def test_failed_compaction_preserves_oversized_fresh_batch_and_checkpoint():
    with workspace():
        messages = history(rounds=4, filler=500)
        messages += tool_pair("fresh", json.dumps(["x" * 100000]))
        cm.atomic(state_paths.path("checkpoint.json"), json.dumps(messages))
        gateway = Gateway(fail=True)
        fork = compaction.ForkCompactor(gateway)
        fork.start(messages)
        fork.wait(5)
        with pytest.raises(cm.ContextError, match="compaction"):
            fork.before_request(messages)
        assert json.loads(state_paths.path("checkpoint.json").read_text()) == messages
        saved = json.loads(state_paths.path("compaction-result.json").read_text())
        assert "replacement" not in saved
        assert json.loads(state_paths.path(saved["source"]).read_text()) == messages


def test_hard_wait_timeout_keeps_running_prefix_identity(monkeypatch):
    with workspace():
        monkeypatch.setitem(cm.TOKEN_CONFIG, "decision_input_tokens", 31000)
        started, release = (threading.Event(), threading.Event())
        calls = []

        def provider(*args, **kwargs):
            calls.append(args)
            started.set()
            assert release.wait(5)
            return completed(GOOD_SUMMARY)

        original = history(rounds=4, filler=5000)
        fork = compaction.ForkCompactor(Gateway(), hard_wait_seconds=0)
        monkeypatch.setattr(fork, "_run", provider)
        fork.start(original)
        assert started.wait(2)
        prefix_hash = compaction.digest(original)
        monkeypatch.setitem(cm.TOKEN_CONFIG, "decision_input_tokens", 11000)
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
        monkeypatch.setitem(cm.TOKEN_CONFIG, "decision_input_tokens", 11000)
        original = [{"role": "user", "content": "goal"}]
        for i in range(6):
            original += [{"role": "user", "content": "u" * 900}] + tool_pair(str(i), "r" * 100)
        fork = compaction.ForkCompactor(Gateway())
        cm.atomic(
            state_paths.path("compaction-result.json"),
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
    config, metric = (cm.TOKEN_CONFIG, cm.metric)
    cm.TOKEN_CONFIG = {
        "decision_input_tokens": 49000,
        "trigger_tokens": 48000,
        "target_tokens": 12000,
        "injected_reserve_tokens": 1000,
        "summary_input_tokens": 491808,
        "summary_reserve_tokens": 5000,
    }
    cm.metric = cm.size
    with tempfile.TemporaryDirectory() as directory:
        os.chdir(directory)
        try:
            yield pathlib.Path(directory)
        finally:
            os.chdir(previous)
            cm.TOKEN_CONFIG, cm.metric = (config, metric)


def completed(text):
    return {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
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


def test_spill_replaces_oversized_result():
    with workspace():
        payload = "x" * 60000
        value = spill.maybe_spill(payload, max_bytes=50000)
        assert isinstance(value, str) and value != payload
        body = json.loads(value)
        assert body["spilled"] is True
        assert body["bytes"] == 60000
        assert body["head_complete"] is False
        assert body["head"] == payload[: spill.HEAD_CHARS]
        assert state_paths.path(body["path"]).read_text(encoding="utf-8") == payload
        assert (
            state_paths.path(body["path"]).resolve().parent
            == state_paths.path(spill.SPILL_DIR).resolve()
        )


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


def test_fork_does_not_block_and_applies_later():
    with workspace():
        gateway = Gateway(delay=0.4)
        compactor = compaction.ForkCompactor(gateway, tools=[], trigger_tokens=2000)
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
        assert applied != messages
        assert cm.message_cost(applied) <= compactor._target_budget(compactor._budget())
        assert applied[0]["content"].startswith(compaction.CHECKPOINT_PREAMBLE)
        assert compaction.CHECKPOINT_OPEN in applied[0]["content"]
        assert cm.paired(applied)


def test_legacy_summary_uses_narrow_instructions_without_main_tools():
    with workspace():
        gateway = Gateway()
        compactor = compaction.ForkCompactor(
            gateway, tools=[{"type": "function", "name": "read"}], trigger_tokens=2000
        )
        messages = history()
        compactor.before_request(messages)
        compactor.wait(timeout=10)
        body = gateway.calls[0]["body"]
        assert set(body) == {"transcript", "source", "max_output_tokens"}
        assert "tools" not in body
        assert "ROUND-0-START" in json.dumps(body)


def test_apply_is_idempotent():
    with workspace():
        compactor = compaction.ForkCompactor(Gateway(), tools=[], trigger_tokens=1000)
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
            Gateway(), tools=[], trigger_tokens=1000, keep_rounds=5
        )
        messages = history(completed_last=False)
        with pytest.raises(cm.ContextError, match="tool call/result"):
            compactor.before_request(messages)
        assert not state_paths.path(compaction.RESULT_PATH).exists()


def test_summary_failure_preserves_original_history_instead_of_building_recall_checkpoint():
    with workspace():
        compactor = compaction.ForkCompactor(Gateway(fail=True), tools=[], trigger_tokens=1000)
        messages = history()
        recall.append_history_many(messages)
        memory.dispatch(
            {
                "action": "write",
                "content": "已确认 2x10 任务需 3200 步，原因是回合长度",
                "kind": "fact",
            },
            root=paths.memory_root(),
        )
        cm.atomic(state_paths.path("checkpoint.json"), json.dumps(messages))
        compactor.before_request(messages)
        compactor.wait(timeout=10)
        with pytest.raises(cm.ContextError, match="compaction"):
            compactor.before_request(messages)
        assert json.loads(state_paths.path("checkpoint.json").read_text()) == messages
        assert "已确认 2x10 任务需 3200 步" in json.dumps(
            memory.dispatch({"action": "list"}, root=paths.memory_root()), ensure_ascii=False
        )
        assert "replacement" not in json.loads(state_paths.path(compaction.RESULT_PATH).read_text())


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


def test_memory_write_then_read_and_list():
    with workspace():
        entry = memory.dispatch(
            {"action": "write", "content": "baseline smolvla 2x5 = 4.2", "kind": "result"},
            root=paths.memory_root(),
        )
        assert entry["id"] and entry["kind"] == "result"
        detail = memory.dispatch(
            {"action": "read", "id": entry["id"][:6]}, root=paths.memory_root()
        )["text"]
        assert "baseline smolvla 2x5 = 4.2" in detail
        listing = json.dumps(
            memory.dispatch({"action": "list"}, root=paths.memory_root()), ensure_ascii=False
        )
        assert entry["id"] in listing


def test_memory_tool_dispatch_and_empty_write():
    with workspace():
        assert json.loads(
            json.dumps(
                memory.dispatch({"action": "write", "content": "note A"}, root=paths.memory_root())
            )
        )["id"]
        assert (
            memory.dispatch({"action": "search", "query": "note A"}, root=paths.memory_root())[
                "items"
            ][0]["preview"]
            == "note A"
        )
        assert "error" in memory.dispatch(
            {"action": "write", "content": "  "}, root=paths.memory_root()
        )


if __name__ == "__main__":
    import traceback

    failures = 0
    for name in sorted(key for key in list(globals()) if key.startswith("test_")):
        try:
            globals()[name]()
            print("PASS", name)
        except Exception:  # noqa: BLE001 - standalone test runner records each failure
            failures += 1
            print("FAIL", name)
            traceback.print_exc()
    print(f"\n{failures} failing")
    raise SystemExit(1 if failures else 0)


def test_token_trigger_does_not_start_at_legacy_byte_threshold(monkeypatch):
    """UTF-8 size alone must not trigger a token-configured background fork."""
    with workspace():
        monkeypatch.setattr(
            cm, "TOKEN_CONFIG", {"trigger_tokens": 25500, "decision_input_tokens": 30000}
        )
        monkeypatch.setattr(cm, "decision_budget", lambda tools: 30000)
        monkeypatch.setattr(cm, "message_cost", lambda messages: cm.size(messages) // 4)
        fork = compaction.ForkCompactor(Gateway())
        started = []
        monkeypatch.setattr(fork, "start", lambda *args, **kwargs: started.append(True))
        messages = [{"role": "user", "content": "a" * 60000}]
        same, _ = fork.before_request(messages)
        assert same == messages and (not started)
        messages = [{"role": "user", "content": "a" * 105000}]
        fork.before_request(messages)
        assert started == [True]


def test_token_trigger_hysteresis_uses_token_cost(monkeypatch):
    with workspace():
        monkeypatch.setattr(
            cm, "TOKEN_CONFIG", {"trigger_tokens": 25500, "decision_input_tokens": 30000}
        )
        monkeypatch.setattr(cm, "decision_budget", lambda tools: 30000)
        monkeypatch.setattr(cm, "message_cost", lambda messages: cm.size(messages) // 4)
        fork = compaction.ForkCompactor(Gateway())
        fork._last_after_tokens = 25000
        started = []
        monkeypatch.setattr(fork, "start", lambda *args, **kwargs: started.append(True))
        fork.before_request([{"role": "user", "content": "a" * 105000}])
        assert not started
        fork.before_request([{"role": "user", "content": "a" * 112000}])
        assert started == [True]


def test_restart_recomputes_token_hysteresis_from_saved_replacement(monkeypatch):
    with workspace():
        monkeypatch.setattr(
            cm, "TOKEN_CONFIG", {"trigger_tokens": 25500, "decision_input_tokens": 30000}
        )
        monkeypatch.setattr(cm, "decision_budget", lambda tools: 30000)
        monkeypatch.setattr(cm, "message_cost", lambda messages: cm.size(messages) // 4)
        replacement = [{"role": "user", "content": "a" * 100000}]
        saved = {
            "version": 2,
            "applied": True,
            "trigger_baseline": {
                "length": len(replacement),
                "sha256": compaction.digest(replacement),
            },
            "after_bytes": cm.size(replacement),
            "replacement": replacement,
            "replacement_sha256": compaction.digest(replacement),
        }
        state_paths.path("compaction-result.json").parent.mkdir(parents=True, exist_ok=True)
        state_paths.path("compaction-result.json").write_text(json.dumps(saved))
        restarted = compaction.ForkCompactor(Gateway())
        started = []
        monkeypatch.setattr(restarted, "start", lambda *args, **kwargs: started.append(True))
        restarted.before_request(replacement + [{"role": "user", "content": "a" * 5000}])
        assert restarted._last_after_tokens == cm.message_cost(replacement)
        assert not started


def test_published_tail_hysteresis_survives_ack_and_restart(monkeypatch):
    with workspace():
        monkeypatch.setattr(
            cm, "TOKEN_CONFIG", {"trigger_tokens": 25500, "decision_input_tokens": 30000}
        )
        monkeypatch.setattr(cm, "decision_budget", lambda tools: 1000)
        monkeypatch.setattr(cm, "message_cost", lambda messages: cm.size(messages) // 4)
        fork = compaction.ForkCompactor(Gateway())
        original = [{"role": "user", "content": "old" * 2000}]
        replacement = [{"role": "user", "content": "s" * 2300}]
        tail = [{"role": "user", "content": "t" * 1150}]
        saved = {
            "version": 2,
            "epoch": fork._epoch(),
            "fork_id": "fixture",
            "prefix_length": len(original),
            "prefix_sha256": compaction.digest(original),
            "source": cm.archive(original, "snapshot"),
            "status": "ready",
            "replacement": replacement,
            "replacement_sha256": compaction.digest(replacement),
            "after_bytes": cm.size(replacement),
            "strategy": "host_summary",
        }
        state_paths.path("compaction-result.json").parent.mkdir(parents=True, exist_ok=True)
        state_paths.path("compaction-result.json").write_text(json.dumps(saved))
        applied, record = fork.before_request(original + tail)
        assert record["kind"] == "applied"
        expected = cm.message_cost(applied)
        cm.atomic(state_paths.path("checkpoint.json"), json.dumps(applied))
        for compactor in (
            fork,
            compaction.ForkCompactor(Gateway()),
            compaction.ForkCompactor(Gateway()),
        ):
            started = []
            monkeypatch.setattr(
                compactor, "start", lambda *args, started=started, **kwargs: started.append(True)
            )
            same, _ = compactor.before_request(applied)
            assert same == applied and (not started)
            assert compactor._last_after_tokens == expected
