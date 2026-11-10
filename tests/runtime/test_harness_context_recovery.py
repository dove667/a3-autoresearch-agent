"""Behavior gates for the derived repair; callbacks replace only the external provider."""

import ast
import importlib.util
import json
import os
import pathlib
import uuid

import pytest

SOURCE = pathlib.Path(__file__).resolve().parents[2] / "src/a3_research_agent/harness"


def load_context():
    spec = importlib.util.spec_from_file_location(
        "context_" + uuid.uuid4().hex, SOURCE / "context/manager.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Small deterministic cost model for offline recovery tests, not a runtime fallback.
    module.TOKEN_CONFIG = {
        "trigger_tokens": 40000,
        "decision_input_tokens": 49000,
        "target_tokens": 12000,
        "injected_reserve_tokens": 1000,
        "summary_input_tokens": 491808,
        "summary_reserve_tokens": 5000,
    }
    module.metric = module.size
    return module


def messages(length=60000):
    return [{"role": "user", "content": "FIRST-CHUNK-SENTINEL " + "a" * length}]


def completed():
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Mock state with completed checks, unknown results and evidence references; no semantic-model claim.",
                    }
                ],
            }
        ],
    }


def test_transient_middle_summary_failure_recovers_in_same_prepare(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    count = 0

    def provider(*args):
        nonlocal count
        count += 1
        return {"http_error": 502} if count == 2 else completed()

    result, record = context.prepare(messages(), [], provider)
    assert result and record and context.paired(result)
    assert count <= 8


def test_completed_summary_is_reused_after_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    result, record = context.prepare(messages(), [], lambda *a: completed())
    restarted = load_context()
    replay, _ = restarted.prepare(messages(), [], lambda *a: pytest.fail("duplicate dispatch"))
    assert replay == result and record["cumulative_summary_calls"] == 1


def test_same_deterministic_budget_error_does_not_spend_again_on_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    def provider(*args):
        calls.append(args)
        return completed()

    first = load_context()
    with pytest.raises(first.ContextError):
        first.prepare(messages(600000), [], provider)
    spent = len(calls)
    restarted = load_context()
    with pytest.raises(restarted.ContextError):
        restarted.prepare(messages(600000), [], provider)
    assert len(calls) == spent


def test_persistent_invalid_summary_has_cross_restart_call_bound(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    def invalid(*args):
        calls.append(args)
        return {"status": "completed", "output": []}

    for _ in range(10):
        context = load_context()
        with pytest.raises(context.ContextError):
            context.prepare(messages(), [], invalid)
    assert len(calls) <= 8


def test_authentication_rejection_is_not_retried_inside_prepare(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    calls = []

    def rejected(*args):
        calls.append(args)
        return {"http_error": 401}

    with pytest.raises(context.ContextError):
        context.prepare(messages(), [], rejected)
    assert len(calls) == 1


def test_malformed_provider_response_does_not_repeat_after_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    with pytest.raises(context.ContextError):
        context.prepare(messages(), [], lambda *a: [])
    restarted = load_context()
    with pytest.raises(restarted.ContextError, match="failed or uncertain"):
        restarted.prepare(messages(), [], lambda *a: pytest.fail("duplicate dispatch"))


def test_corrupted_summary_record_does_not_allow_repeat_dispatch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    context.prepare(messages(), [], lambda *a: completed())
    path = next(context.ROOT.glob("summary-request-*.json"))
    state = json.loads(path.read_text())
    state["fingerprint"] = "damaged"
    path.write_text(json.dumps(state))
    restarted = load_context()
    with pytest.raises(restarted.ContextError, match="checksum"):
        restarted.prepare(messages(), [], lambda *a: pytest.fail("duplicate dispatch"))


def agent_functions(context):
    from a3_research_agent.harness.memory import state

    source = ast.parse((SOURCE / "agent.py").read_text())
    wanted = {"save_manifest", "checkpoint", "checkpoint_hash", "recover_journal"}
    nodes = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    namespace = {
        "context_manager": context,
        "memory_state": state,
        "pathlib": pathlib,
        "os": os,
        "json": json,
        "hashlib": __import__("hashlib"),
    }
    exec(  # noqa: S102 - execute selected local definitions, never the Agent entry loop
        compile(ast.Module(body=nodes, type_ignores=[]), "agent-functions-under-test", "exec"),
        namespace,
    )
    return namespace


def initial_state(tmp_path):
    old = [{"role": "user", "content": "last committed state"}]
    (tmp_path / "checkpoint.json").write_text(json.dumps(old))
    (tmp_path / "pending-calls.json").write_text("null")
    names = ["checkpoint.json", "pending-calls.json", "resume.json", "continuation.json"]
    for name in ["resume.json", "continuation.json"]:
        (tmp_path / name).write_text(
            json.dumps({"command": ["python", "agent.py", "--resume"], "files": names})
        )
    (tmp_path / "new-history.json").write_text("new archive contents")
    return old


@pytest.mark.parametrize("target", ["resume.json", "continuation.json"])
def test_manifest_failure_leaves_last_committed_checkpoint_usable(tmp_path, monkeypatch, target):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    functions = agent_functions(context)
    old = initial_state(tmp_path)
    replace = os.replace

    def fail_manifest(source, destination):
        if pathlib.Path(destination).name == target:
            raise OSError("controlled storage failure")
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_manifest)
    with pytest.raises(OSError):
        functions["checkpoint"]([{"role": "user", "content": "Source: new-history.json"}])
    assert json.loads((tmp_path / "checkpoint.json").read_text()) == old
    for name in ["resume.json", "continuation.json"]:
        manifest = json.loads((tmp_path / name).read_text())
        assert all((tmp_path / p).is_file() for p in manifest["files"])


def test_clearing_resolved_journal_does_not_invalidate_resume_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    functions = agent_functions(context)
    old = initial_state(tmp_path)
    (tmp_path / "pending-calls.json").write_text(
        json.dumps({"base_hash": "earlier-checkpoint", "output": [], "results": {}})
    )
    assert functions["recover_journal"](old) == old
    manifest = json.loads((tmp_path / "resume.json").read_text())
    assert all((tmp_path / p).is_file() for p in manifest["files"])
    assert json.loads((tmp_path / "pending-calls.json").read_text()) is None


def test_manifest_flush_failure_does_not_publish_new_checkpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    functions = agent_functions(context)
    old = initial_state(tmp_path)
    fsync = os.fsync

    def fail_manifest_flush(descriptor):
        temporaries = list(tmp_path.glob(".resume.json-*.tmp"))
        if any(p.stat().st_ino == os.fstat(descriptor).st_ino for p in temporaries):
            raise OSError("controlled manifest flush failure")
        return fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_manifest_flush)
    with pytest.raises(OSError):
        functions["checkpoint"]([{"role": "user", "content": "Source: new-history.json"}])
    assert json.loads((tmp_path / "checkpoint.json").read_text()) == old


def test_summary_headroom_does_not_recursively_split_a_feasible_plan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    calls = []

    def provider(*args):
        calls.append(args)
        response = completed()
        response["output"][0]["content"][0]["text"] = "Preserved mock state. " + "x" * 5000
        return response

    result, record = context.prepare(messages(90000), [], provider)
    assert result and context.paired(result)
    assert record["cumulative_summary_calls"] <= 8
    assert len(calls) <= 8


def test_oversized_summary_is_diagnosed_and_restart_is_bounded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    def provider(*args):
        calls.append(args)
        response = completed()
        response["output"][0]["content"][0]["text"] = "Unbounded summary. " + "x" * 12000
        return response

    for _ in range(10):
        context = load_context()
        with pytest.raises(context.ContextError):
            context.prepare(messages(50000), [], provider)
    assert len(calls) <= 8
    failure_records = list((tmp_path / "context-history").glob("summary-failure-*.json"))
    assert any(
        json.loads(p.read_text())["reason"] == "summary_text_exceeds_memory_budget"
        for p in failure_records
    )


def test_large_output_archival_alone_avoids_summary_and_keeps_original(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    original = [
        {"role": "user", "content": "Keep the unknown result unknown."},
        {"type": "function_call", "name": "read", "call_id": "read-large", "arguments": "{}"},
        {
            "type": "function_call_output",
            "call_id": "read-large",
            "output": "reference-data " * 10000,
        },
    ]
    result = context.fit_tool_outputs(original, context.decision_budget([]))
    assert context.paired(result)
    reference = json.loads(result[-1]["output"])["path"]
    assert json.loads(pathlib.Path(reference).read_text()) == original[-1]


def test_token_mode_uses_host_reserve_and_archive_fast_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    config = {
        "enabled": True,
        "unit": "estimated_tokens",
        "tokenizer": "o200k_base",
        "trigger_tokens": 6000,
        "decision_input_tokens": 6000,
        "target_tokens": 3000,
        "summary_input_tokens": 491808,
        "injected_reserve_tokens": 1000,
        "summary_reserve_tokens": 512,
        "main_context_tokens": 500000,
        "summary_context_tokens": 500000,
        "feedback_refs": [],
    }
    context.metric = __import__(
        "a3_research_agent.token_count", fromlist=["count_tokens"]
    ).count_tokens
    context.configure(lambda method, path, data=None: config)
    original = [
        {"type": "function_call", "call_id": "r", "name": "read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "r", "output": "read reference data " * 20000},
    ]
    result = context.fit_tool_outputs(original, context.decision_budget([]))
    assert context.paired(result)
    assert context.message_cost(result) <= context.decision_budget([])


def test_token_compaction_uses_summary_operation_and_preserves_pairing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    config = {
        "enabled": True,
        "unit": "estimated_tokens",
        "tokenizer": "o200k_base",
        "trigger_tokens": 6000,
        "decision_input_tokens": 6000,
        "target_tokens": 3000,
        "summary_input_tokens": 491808,
        "injected_reserve_tokens": 1000,
        "summary_reserve_tokens": 512,
        "main_context_tokens": 500000,
        "summary_context_tokens": 500000,
        "feedback_refs": [],
    }
    context.metric = __import__(
        "a3_research_agent.token_count", fromlist=["count_tokens"]
    ).count_tokens
    context.configure(lambda method, path, data=None: config)
    requests = []

    def provider(method, path, body):
        requests.append((method, path, body))
        return completed()

    result, record = context.prepare(messages(80000), [], provider)
    assert context.paired(result)
    assert all(path == "/v1/summary" for _, path, _ in requests)
    assert all(set(body) == {"transcript", "source"} for _, _, body in requests)
    assert record["budget_unit"] == "estimated_tokens" and record["after_estimated_tokens"] < 6000
    assert record["after_estimated_tokens"] < record["before_estimated_tokens"]


def test_temporary_config_error_does_not_silently_select_legacy_mode():
    context = load_context()
    with pytest.raises(context.ContextError):
        context.metric = __import__(
            "a3_research_agent.token_count", fromlist=["count_tokens"]
        ).count_tokens
        context.configure(lambda *args: {"http_error": 503})


@pytest.mark.parametrize("failure", [{"http_error": 502}, {"status": "completed", "output": []}])
def test_token_mode_persistent_failure_remains_bounded_across_restart(
    tmp_path, monkeypatch, failure
):
    monkeypatch.chdir(tmp_path)
    config = {
        "enabled": True,
        "unit": "estimated_tokens",
        "tokenizer": "o200k_base",
        "trigger_tokens": 6000,
        "decision_input_tokens": 6000,
        "target_tokens": 3000,
        "summary_input_tokens": 491808,
        "injected_reserve_tokens": 1000,
        "summary_reserve_tokens": 512,
        "main_context_tokens": 500000,
        "summary_context_tokens": 500000,
        "feedback_refs": [],
    }
    requests = []
    for _ in range(10):
        context = load_context()
        context.metric = __import__(
            "a3_research_agent.token_count", fromlist=["count_tokens"]
        ).count_tokens
        context.configure(lambda *args: config)
        with pytest.raises(context.ContextError):
            context.prepare(messages(80000), [], lambda *args: requests.append(args) or failure)
    assert len(requests) <= 8


def test_escaping_heavy_summary_is_sized_like_host_envelope(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from a3_research_agent.host.gateway.app import create_app

    monkeypatch.chdir(tmp_path)
    provider_calls = []
    client = TestClient(
        create_app(
            model_name="fixed/main",
            sessions={"token": "main"},
            main_owner="main",
            usage_log=tmp_path / "usage.jsonl",
            remote_call=lambda *args: {},
            forward_model=lambda **kw: provider_calls.append(kw) or completed(),
            context_budget={
                "main_context_tokens": 500000,
                "worker_context_tokens": 500000,
                "summary_context_tokens": 500000,
                "trigger_tokens": 64000,
                "target_tokens": 32000,
            },
        )
    )
    rejected = []

    def request(method, path, data=None):
        response = client.request(
            method, path, headers={"Authorization": "Bearer token"}, json=data
        )
        if response.status_code >= 400:
            rejected.append(response.status_code)
            return {"http_error": response.status_code}
        return response.json()

    context = load_context()
    context.metric = __import__(
        "a3_research_agent.token_count", fromlist=["count_tokens"]
    ).count_tokens
    context.configure(request)
    original = [{"role": "user", "content": '\\" ' * 40000}] + [
        {"role": "user", "content": "recent fixed fact"} for _ in range(6)
    ]
    # Exercise summary-envelope sizing at an explicit small fixture budget;
    # the new Host's independent hard admission would let this history fit.
    limit = context.TOKEN_CONFIG["trigger_tokens"] - context.TOKEN_CONFIG["injected_reserve_tokens"]
    assert context.message_cost(original) > limit
    result, record = context.prepare(original, [], request, budget_limit=limit)
    assert context.paired(result) and not rejected
    assert provider_calls and len(provider_calls) <= 8
    assert record["after_estimated_tokens"] < record["before_estimated_tokens"]


def test_overlong_summary_keeps_source_without_corrective_retry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    calls = []

    def provider(*args):
        calls.append(args[-1])
        result = completed()
        result["output"][0]["content"][0]["text"] = "汉" * 2100
        return result

    with pytest.raises(context.ContextError, match="memory_budget"):
        context.prepare(messages(), [], provider)
    assert len(calls) == 1
    assert json.loads(pathlib.Path(calls[0]["source"]).read_text()) == messages()


def test_failed_summary_remains_failed_after_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    with pytest.raises(context.ContextError):
        context.prepare(messages(), [], lambda *a: {"http_error": 502})
    restarted = load_context()
    with pytest.raises(restarted.ContextError, match="failed or uncertain"):
        restarted.prepare(messages(), [], lambda *a: pytest.fail("unexpected retry"))


def test_persistently_overlong_summary_still_blocks_with_original_evidence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []
    original = messages()

    def invalid(method, path, body):
        calls.append(body)
        result = completed()
        result["output"][0]["content"][0]["text"] = "汉" * 2100
        return result

    for _ in range(5):
        context = load_context()
        with pytest.raises(context.ContextError):
            context.prepare(original, [], invalid)
    assert len(calls) <= 8
    assert len(calls) == 1
    assert json.loads(pathlib.Path(calls[0]["source"]).read_text()) == original


def test_unconsumed_tool_result_fits_alone_and_reaches_next_decision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    context.TOKEN_CONFIG["decision_input_tokens"] = 9000
    result_text = "CURRENT-OBSERVATION: experiment rejected; evidence at result.json. " + "x" * 5000
    original = [
        {"role": "user", "content": "old background " * 600},
        {"type": "function_call", "name": "read", "call_id": "fresh", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "fresh", "output": result_text},
    ]
    result, _ = context.prepare(original, [], lambda *args: completed())
    assert result[-1]["output"] == result_text
    assert context.paired(result)


def test_complete_fresh_tool_batch_is_not_summarized_to_meet_soft_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    context.TOKEN_CONFIG["decision_input_tokens"] = 11000
    calls = [
        {"type": "function_call", "name": "read", "call_id": str(i), "arguments": "{}"}
        for i in range(8)
    ]
    outputs = [
        {"type": "function_call_output", "call_id": str(i), "output": f"FRESH-{i} " + "z" * 500}
        for i in range(8)
    ]
    original = [{"role": "user", "content": "old " * 2200}] + calls + outputs
    result, _ = context.prepare(original, [], lambda *args: completed())
    assert result[-len(calls + outputs) :] == calls + outputs
    assert context.paired(result)


def test_oversized_result_archive_has_bounded_preview_and_lossless_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    original = [
        {"type": "function_call", "name": "read", "call_id": "large", "arguments": "{}"},
        {
            "type": "function_call_output",
            "call_id": "large",
            "output": "START-FACT " + "m" * 150000 + " END-FACT",
        },
    ]
    result = context.fit_tool_outputs(original, context.decision_budget([]))
    view = json.loads(result[-1]["output"])
    assert "START-FACT" in view["preview"] and "END-FACT" in view["preview"]
    assert view["preview_complete"] is False
    assert json.loads(pathlib.Path(view["path"]).read_text()) == original[-1]
    assert len(result[-1]["output"].encode()) < 12000


def test_repeated_read_compact_cycles_deliver_new_observation_without_closing_task(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    context.TOKEN_CONFIG["decision_input_tokens"] = 13000
    state = [
        {"role": "user", "content": "Task remains open until the Agent decides from evidence."}
    ]
    for i in range(24):
        fact = f"CURRENT-RESULT-{i}: observed but not a passed acceptance. " + "k" * 5000
        state += [
            {"type": "function_call", "name": "read", "call_id": str(i), "arguments": "{}"},
            {"type": "function_call_output", "call_id": str(i), "output": fact},
        ]
        state, _ = context.prepare(state, [], lambda *args: completed())
        assert state[-1]["output"] == fact
        assert context.paired(state)
    assert not (tmp_path / "run-outcome.json").exists()
    assert not (tmp_path / "decision.json").exists()


def test_checkpoint_publication_preserves_declared_handoff_metadata(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    for name in ["resume.json", "continuation.json"]:
        (tmp_path / name).write_text(
            json.dumps(
                {
                    "command": ["python", "agent.py", "--resume"],
                    "files": [],
                    "remote_exports": ["export-example"],
                    "provenance": {"scope": "untested research state"},
                }
            )
        )
    agent_functions(context)["save_manifest"]()
    for name in ["resume.json", "continuation.json"]:
        saved = json.loads((tmp_path / name).read_text())
        assert saved["remote_exports"] == ["export-example"]
        assert saved["provenance"] == {"scope": "untested research state"}


def test_invalid_existing_manifest_does_not_silently_reset_handoff_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    (tmp_path / "continuation.json").write_text("{bad")
    with pytest.raises(ValueError):
        agent_functions(context)["save_manifest"]()
    assert (tmp_path / "continuation.json").read_text() == "{bad"


def test_protected_batch_does_not_carry_avoidable_old_tail(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    context.TOKEN_CONFIG["decision_input_tokens"] = 13000
    original = [{"role": "user", "content": "old " * 3250}]
    for i in range(2):
        original += [
            {"type": "function_call", "name": "read", "call_id": str(i), "arguments": "{}"},
            {"type": "function_call_output", "call_id": str(i), "output": "x" * 2400},
        ]
    fresh = [
        {"type": "function_call", "name": "read", "call_id": "fresh", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "fresh", "output": "CURRENT " + "z" * 7500},
    ]
    result, _ = context.prepare(original + fresh, [], lambda *args: completed())
    assert result[-2:] == fresh
    assert context.message_cost(result) <= context.decision_budget([])


def test_summary_does_not_spend_calls_on_opaque_provider_ciphertext(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    original = [
        {
            "role": "user",
            "content": "Confirmed decision rejected; source decision.json, no re-audit requested.",
        },
        {
            "type": "reasoning",
            "encrypted_content": "OPAQUE-CIPHER-SENTINEL-" * 16000,
            "summary": [],
        },
    ]
    original += [{"role": "user", "content": "Later bounded observation."} for _ in range(6)]
    requests = []
    result, record = context.prepare(
        original, [], lambda *args: requests.append(args[-1]) or completed()
    )
    assert len(requests) == 1
    assert "OPAQUE-CIPHER-SENTINEL" not in json.dumps(requests)
    assert json.loads(pathlib.Path(record["source"]).read_text()) == original
    assert result


def test_token_soft_target_does_not_swallow_fresh_batch_with_tools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    context.TOKEN_CONFIG = {
        "trigger_tokens": 64000,
        "decision_input_tokens": 64000,
        "target_tokens": 32000,
        "summary_input_tokens": 491808,
        "injected_reserve_tokens": 20000,
        "summary_reserve_tokens": 2000,
        "feedback_refs": [],
    }
    context.metric = lambda value: context.size(value) // 4 + 1
    tools = [{"type": "function", "name": "read", "description": "bounded read " * 100}]
    fresh = [
        {"type": "reasoning", "encrypted_content": "opaque-new-turn-state"},
        {"type": "function_call", "name": "read", "call_id": "fresh", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "fresh", "output": "NEW DATA " * 4500},
    ]
    original = [{"role": "user", "content": "old history " * 20000}] + fresh
    result, record = context.prepare(original, tools, lambda *args: completed())
    assert result[-3:] == fresh
    assert context.paired(result)
    assert context.message_cost(result) <= context.decision_budget(tools)
    assert record["budget_unit"] == "estimated_tokens"


def test_control_character_preview_is_bounded_without_mutating_archive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    original = {"type": "function_call_output", "call_id": "x", "output": "\x00😀" * 20000}
    view = context.archived_output(original, 2048)
    assert context.size(view) <= 2048
    assert json.loads(pathlib.Path(json.loads(view["output"])["path"]).read_text()) == original


@pytest.mark.parametrize("broken", ["{bad", "[]"])
def test_invalid_second_manifest_keeps_both_manifests_and_checkpoint(tmp_path, monkeypatch, broken):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    (tmp_path / "resume.json").write_text(
        json.dumps({"files": [], "remote_exports": ["export-kept"]})
    )
    (tmp_path / "continuation.json").write_text(broken)
    (tmp_path / "checkpoint.json").write_text('[{"role":"user","content":"last valid state"}]')
    before = {
        name: (tmp_path / name).read_bytes()
        for name in ["resume.json", "continuation.json", "checkpoint.json"]
    }
    with pytest.raises((ValueError, TypeError)):
        agent_functions(context)["checkpoint"]([{"role": "user", "content": "new state"}])
    assert all((tmp_path / name).read_bytes() == raw for name, raw in before.items())


def test_changed_reserve_reuses_same_full_transcript_answer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    context.prepare(messages(), [], lambda *a: completed())
    context.TOKEN_CONFIG["decision_input_tokens"] = 47000
    result, _ = context.prepare(
        messages(), [], lambda *a: pytest.fail("reserve change redispatched")
    )
    assert result


def test_token_target_keeps_useful_tail_instead_of_fixed_six_messages(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    context.TOKEN_CONFIG = {
        "trigger_tokens": 20000,
        "decision_input_tokens": 30000,
        "target_tokens": 16000,
        "injected_reserve_tokens": 1000,
        "summary_input_tokens": 491808,
        "summary_reserve_tokens": 1000,
    }
    original = [
        {"role": "user", "content": f"Historical turn {index}: " + "word " * 250}
        for index in range(160)
    ]
    calls = []

    def summarize(*args):
        calls.append(args)
        return completed()

    result, record = context.prepare(original, [], summarize, budget_limit=17000)
    retained = result[1:]
    assert len(retained) >= 6
    assert retained == original[-len(retained) :]
    assert context.paired(result) and context.message_cost(result) <= 17000
    assert record and len(calls) == 1


def test_token_tail_search_preserves_pairs_without_linear_tokenizer_passes(monkeypatch):
    context = load_context()
    messages = [{"role": "user", "content": "goal"}]
    for index in range(1000):
        messages.extend(
            [
                {"type": "function_call", "call_id": str(index)},
                {"type": "function_call_output", "call_id": str(index), "output": "result"},
            ]
        )
    counts = []

    def count(candidate):
        counts.append(len(candidate))
        return 100 * len(candidate)

    monkeypatch.setattr(context, "message_cost", count)
    cut = context._token_tail_boundary(messages, 10000, len(messages) - 2)
    assert cut is not None and cut <= len(messages) - 2
    assert context.paired(messages[:cut]) and context.paired(messages[cut:])
    assert len(messages[cut:]) == 100
    assert len(counts) <= 11  # 1,000 safe boundaries need at most ten binary-search comparisons.
