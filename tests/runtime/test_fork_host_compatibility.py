"""Exercise real Host validation with fake providers, never live inference."""

import importlib.util
import json
import pathlib
import sys
import threading

import pytest
from fastapi.testclient import TestClient

from runtime.local.host_gateway import create_app
from runtime.local.human_feedback import HumanFeedback

SOURCE = pathlib.Path(__file__).resolve().parents[2] / "runtime/harness_recovery"


@pytest.fixture
def modules(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(SOURCE))
    loaded = {}
    for name in ("context_manager", "recall", "spill", "compaction"):
        spec = importlib.util.spec_from_file_location(name, SOURCE / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded[name] = module
    return loaded


def tool_history(count=20, length=4000):
    messages = [{"role": "user", "content": "Preserve this scientific goal and its constraints."}]
    for i in range(count):
        messages.extend(
            [
                {"type": "function_call", "call_id": str(i), "name": "read", "arguments": "{}"},
                {
                    "type": "function_call_output",
                    "call_id": str(i),
                    "output": "evidence " + "x" * length,
                },
            ]
        )
    return messages


def test_real_host_summary_protocol_role_and_feedback(modules, tmp_path):
    calls = []
    feedback = HumanFeedback(tmp_path)
    feedback.register_content(b"PRIVATE-OPERATOR-NOTICE", "main")

    def forward(**kwargs):
        calls.append(kwargs)
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Goal retained. Evidence remains uncertain; preserve source and continue verification.",
                        }
                    ],
                }
            ],
        }

    client = TestClient(
        create_app(
            model_name="main-model",
            main_owner="main",
            sessions={"token": "main"},
            forward_model=forward,
            remote_call=lambda *a: {},
            usage_log=tmp_path / "usage.jsonl",
            phase_instructions="PRIVATE-PHASE",
            human_feedback=feedback,
            role_models={
                "summary": {
                    "model_name": "summary-model",
                    "request_options": {"reasoning": {"effort": "medium"}},
                }
            },
        )
    )
    paths = []

    def req(method, path, data=None):
        paths.append(path)
        response = client.request(
            method, path, headers={"Authorization": "Bearer token"}, json=data
        )
        return (
            response.json() if response.status_code < 400 else {"http_error": response.status_code}
        )

    cm, fork_module = modules["context_manager"], modules["compaction"]
    cm.configure(req)
    fork = fork_module.ForkCompactor(req, trigger_bytes=2000)
    original = tool_history()
    fork.before_request(original)
    fork.wait(5)
    result, _ = fork.before_request(original)
    assert calls, "real Host must accept the summary request"
    assert all(call["model"] == "summary-model" for call in calls)
    assert all("tools" not in call and call["max_output_tokens"] == 1000 for call in calls)
    assert "PRIVATE-OPERATOR-NOTICE" not in json.dumps(calls)
    assert "PRIVATE-PHASE" not in json.dumps(calls)
    assert "/v1/responses" not in paths
    assert cm.size(result) < cm.size(original) and cm.paired(result)


def test_initial_user_plus_continuous_tools_has_bounded_tail(modules):
    original = tool_history()
    fork = modules["compaction"].ForkCompactor(lambda *a: {})
    tail = fork._tail(original)
    assert len(tail) < len(original)
    assert modules["context_manager"].paired(tail)


def test_stale_result_cannot_replace_new_task(modules):
    pathlib.Path("compaction-result.json").write_text(
        json.dumps(
            {
                "applied": False,
                "prefix_length": 99,
                "checkpoint": "OLD TASK",
                "tail": [],
            }
        )
    )
    original = [{"role": "user", "content": "NEW TASK MUST SURVIVE"}]
    fork = modules["compaction"].ForkCompactor(lambda *a: {})
    result, _ = fork.before_request(original)
    assert result == original


def test_resume_history_reconciliation_is_idempotent(modules):
    recall = modules["recall"]
    original = tool_history(3, 10)
    recall.append_history(original[-1])
    recall.ensure_history(original)
    first = pathlib.Path("history.jsonl").read_bytes()
    recall.ensure_history(original)
    assert pathlib.Path("history.jsonl").read_bytes() == first
    assert all(message in recall.load_history() for message in original)


def test_hard_budget_wait_is_bounded_and_never_returns_oversize(modules):
    cm = modules["context_manager"]
    cm.WINDOW, cm.RESERVE = 10000, 4000
    release = threading.Event()

    def provider(*args):
        assert release.wait(5)
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Goals retained; findings remain unknown until verified from original source.",
                        }
                    ],
                }
            ],
        }

    fork = modules["compaction"].ForkCompactor(provider, hard_wait_seconds=0)
    try:
        with pytest.raises(cm.ContextError):
            fork.before_request(
                tool_history(), extra_messages=[{"role": "user", "content": "reserve " * 100}]
            )
    finally:
        release.set()
        fork.wait(5)
    assert not fork.status()["busy"]


def test_replacement_replays_after_crash_before_checkpoint_and_acknowledges_after(modules):
    cm, module = modules["context_manager"], modules["compaction"]
    calls = []

    def provider(*args):
        calls.append(args)
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Original goals retained; unknown results still require direct evidence verification.",
                        }
                    ],
                }
            ],
        }

    original = tool_history(10, 700)
    pathlib.Path("checkpoint.json").write_text(json.dumps(original))
    fork = module.ForkCompactor(provider, trigger_bytes=1000)
    fork.before_request(original)
    fork.wait(5)
    first, record = fork.before_request(original)
    assert record["kind"] == "applied"
    assert json.loads(pathlib.Path(module.RESULT_PATH).read_text())["applied"] is False
    restarted = module.ForkCompactor(provider, trigger_bytes=1000)
    second, record = restarted.before_request(original)
    assert second == first and record["kind"] == "applied"
    count = len(calls)
    pathlib.Path("checkpoint.json").write_text(json.dumps(first))
    restarted.before_request(first)
    assert len(calls) == count
    assert json.loads(pathlib.Path(module.RESULT_PATH).read_text())["applied"] is True
    assert all(message in modules["recall"].load_history() for message in original)
    assert cm.paired(first)


def test_valid_result_is_rejected_after_source_prefix_change(modules):
    module = modules["compaction"]
    original = tool_history(10, 700)
    fork = module.ForkCompactor(lambda *a: {"http_error": 422}, trigger_bytes=1000)
    fork.before_request(original)
    fork.wait(5)
    changed = [{"role": "user", "content": "A different scientific goal"}] + original[1:]
    # Inspect application only: the subsequent soft scheduling is a separate decision.
    kept, record = fork._apply_if_ready(changed)
    assert kept == changed and record["reason"] == "source_identity_mismatch"
    assert list(pathlib.Path("context-history").glob("rejected-compaction-result-*.json"))


def test_interrupted_fork_preserves_source_without_replaying_unknown_call(modules):
    module, cm = modules["compaction"], modules["context_manager"]
    original = tool_history(6, 100)
    fork = module.ForkCompactor(lambda *a: (_ for _ in ()).throw(AssertionError("must not retry")))
    source = cm.archive(original, "snapshot")
    pathlib.Path(module.RESULT_PATH).write_text(
        json.dumps(
            {
                "version": 2,
                "status": "running",
                "applied": False,
                "fork_id": "synthetic",
                "epoch": fork._epoch(),
                "prefix_length": len(original),
                "prefix_sha256": module.digest(original),
                "source": source,
            }
        )
    )
    restored, report = fork.before_request(original)
    assert report["strategy"] == "interrupted"
    assert cm.paired(restored)
    assert "not summarized" in json.dumps(restored)
    assert json.loads(pathlib.Path(source).read_text()) == original


def test_history_repairs_torn_tail_preserves_duplicate_occurrences_and_checks_sources(modules):
    recall = modules["recall"]
    repeated = {"role": "user", "content": "repeat the same observation"}
    pathlib.Path("history.jsonl").write_text('{"partial":')
    first = recall.ensure_history([repeated, repeated])
    assert first["corrupt_lines"] == 1 and first["appended"] == 2
    assert recall.ensure_history([repeated, repeated])["appended"] == 0
    assert recall.load_history() == [repeated, repeated]
    missing = "context-history/snapshot-" + "a" * 64 + ".json"
    with pytest.raises(ValueError, match="archive"):
        recall.ensure_history(
            [
                {
                    "role": "user",
                    "content": "Working memory derived from archived untrusted conversation; Source: "
                    + missing,
                }
            ]
        )


def test_real_token_host_budget_includes_tools_and_operational_frame(
    modules, tmp_path, monkeypatch
):
    from runtime.local import token_count

    monkeypatch.setitem(sys.modules, "token_count", token_count)
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Goals and constraints preserved. Observations remain unverified until checked against sources.",
                        }
                    ],
                }
            ],
        }

    budget = {
        "main_context_tokens": 100000,
        "worker_context_tokens": 100000,
        "summary_context_tokens": 100000,
        "trigger_tokens": 30000,
        "target_tokens": 12000,
        "summary_input_tokens": 20000,
    }
    client = TestClient(
        create_app(
            model_name="main-model",
            main_owner="main",
            sessions={"token": "main"},
            forward_model=provider,
            remote_call=lambda *a: {},
            usage_log=tmp_path / "usage.jsonl",
            context_budget=budget,
            role_models={"summary": {"model_name": "summary-model", "request_options": {}}},
        )
    )

    def req(method, path, data=None):
        response = client.request(
            method, path, json=data, headers={"Authorization": "Bearer token"}
        )
        return (
            response.json() if response.status_code < 400 else {"http_error": response.status_code}
        )

    cm, module = modules["context_manager"], modules["compaction"]
    cm.configure(req)
    tools = [
        {"type": "function", "name": "read", "parameters": {"type": "object", "properties": {}}}
    ]
    extra = [{"role": "user", "content": "operational reserve " * 1000}]
    fork = module.ForkCompactor(req, tools=tools, trigger_bytes=1000)
    original = tool_history(20, 2000)
    fork.before_request(original, extra_messages=extra)
    fork.wait(5)
    prepared, record = fork.before_request(original, extra_messages=extra)
    assert record["kind"] == "applied"
    assert cm.message_cost(prepared) + cm.message_cost(extra) <= cm.decision_budget(tools)
    assert calls and all(call["model"] == "summary-model" for call in calls)
    assert (
        client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer token"},
            json={"input": prepared + extra, "tools": tools},
        ).status_code
        == 200
    )
    assert calls[-1]["model"] == "main-model"


def test_extra_messages_and_legacy_extra_bytes_cannot_consume_tool_reserve(modules):
    cm = modules["context_manager"]
    cm.WINDOW, cm.RESERVE = 5000, 2000
    tools = [{"type": "function", "name": "probe", "description": "tool schema " * 50}]
    fork = modules["compaction"].ForkCompactor(lambda *a: {}, tools=tools)
    assert fork._budget(extra_bytes=400) == cm.decision_budget(tools) - 400
    extra = [{"role": "user", "content": "operational " * 50}]
    assert fork._budget(extra_messages=extra) == cm.decision_budget(tools) - cm.message_cost(extra)
    with pytest.raises(cm.ContextError, match="reserves"):
        fork.before_request(
            [{"role": "user", "content": "small goal"}],
            extra_messages=[{"role": "user", "content": "x" * 5000}],
        )


def test_unpaired_candidate_or_missing_original_is_never_applied(modules):
    cm, module = modules["context_manager"], modules["compaction"]
    original = tool_history(6, 100)
    source = cm.archive(original, "snapshot")
    fork = module.ForkCompactor(lambda *a: {})
    replacement = [{"type": "function_call_output", "call_id": "orphan", "output": "unsafe"}]
    result = {
        "version": 2,
        "status": "ready",
        "applied": False,
        "fork_id": "synthetic",
        "epoch": fork._epoch(),
        "prefix_length": len(original),
        "prefix_sha256": module.digest(original),
        "source": source,
        "replacement": replacement,
        "replacement_sha256": module.digest(replacement),
        "after_bytes": cm.size(replacement),
    }
    pathlib.Path(module.RESULT_PATH).write_text(json.dumps(result))
    with pytest.raises(cm.ContextError, match="paired"):
        fork.before_request(original)
    pathlib.Path(source).unlink()
    with pytest.raises(cm.ContextError, match="archive"):
        fork.before_request(original)


def test_json_null_result_and_fresh_epoch_are_supported(modules):
    module = modules["compaction"]
    pathlib.Path(module.RESULT_PATH).write_text("null")
    pathlib.Path(module.EPOCH_PATH).write_text(json.dumps({"version": 1, "id": None}))
    fork = module.ForkCompactor(lambda *a: {})
    goal = [{"role": "user", "content": "fresh goal"}]
    assert fork.before_request(goal) == (goal, None)
    assert len(fork._epoch()) == 32


def test_missing_spill_reference_is_not_a_successful_history_backfill(modules):
    payload = modules["spill"].maybe_spill("original evidence " * 5000)
    source = json.loads(payload)["path"]
    pathlib.Path(source).unlink()
    # Agent serializes a string tool result once more into function_call_output.
    message = {"type": "function_call_output", "call_id": "spill", "output": json.dumps(payload)}
    with pytest.raises(ValueError, match="original"):
        modules["recall"].ensure_history([message])


def test_shutdown_apply_ready_never_starts_a_fork_or_provider(modules):
    calls = []
    module = modules["compaction"]
    fork = module.ForkCompactor(lambda *args: calls.append(args), trigger_bytes=1)
    original = tool_history(4, 100)
    prepared, report = fork.apply_ready(original)
    assert prepared == original and report is None
    assert calls == [] and not pathlib.Path(module.RESULT_PATH).exists()
    assert not fork.status()["busy"]


def test_archive_only_preserves_original_goal_and_worker_assignment_bytes(modules):
    cm, module = modules["context_manager"], modules["compaction"]
    original = tool_history(1, 20000)
    original[0]["content"] = (
        "Worker assignment: 只验证指定证据。\nScientific goal: preserve scope; do not change acceptance."
    )
    goal_bytes = original[0]["content"].encode("utf-8")

    def forbidden_provider(*args):
        raise AssertionError("Archiving one large tool output must not need a model")

    fork = module.ForkCompactor(forbidden_provider, trigger_bytes=1000)
    fork.before_request(original)
    fork.wait(5)
    prepared, report = fork.before_request(original)
    assert report["kind"] == "applied"
    assert report["strategy"] == "tool_outputs_archived"
    assert cm.size(prepared) < cm.size(original) and cm.paired(prepared)
    assert prepared[0] == original[0]
    assert prepared[0]["content"].encode("utf-8") == goal_bytes


def test_hard_limit_waits_for_slow_summary_without_oversize_decision(modules, monkeypatch):
    """Simulate a 60s summary deadline without sleeping for a minute."""
    cm, module = modules["context_manager"], modules["compaction"]
    cm.WINDOW, cm.RESERVE = 10000, 4000
    release = threading.Event()

    def provider(*args):
        assert release.wait(5)
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Keep original goal; source evidence remains authoritative. Pending work is not complete.",
                        }
                    ],
                }
            ],
        }

    fork = module.ForkCompactor(provider)
    actual_wait = fork.wait

    def virtual_wait(timeout=None):
        if timeout is not None and timeout >= 60:
            release.set()
            actual_wait(5)

    monkeypatch.setattr(fork, "wait", virtual_wait)
    try:
        result, record = fork.before_request(tool_history())
        assert cm.paired(result) and cm.message_cost(result) <= fork._budget()
        assert record["kind"] == "applied"
    finally:
        release.set()
        actual_wait(5)
