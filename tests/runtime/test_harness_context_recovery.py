"""Behavior gates for the derived repair; callbacks replace only the external provider."""

import ast
import importlib.util
import json
import os
import pathlib
import uuid
from types import SimpleNamespace

import pytest

SOURCE = pathlib.Path(
    os.environ.get(
        "HARNESS_SOURCE",
        str(pathlib.Path(__file__).resolve().parents[2] / "runtime/harness_recovery"),
    )
)


def load_context():
    spec = importlib.util.spec_from_file_location(
        "context_" + uuid.uuid4().hex, SOURCE / "context_manager.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.time = SimpleNamespace(time=module.time.time, sleep=lambda _: None)
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


def test_completed_summary_prefix_survives_default_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    count = 0

    def interrupted(*args):
        nonlocal count
        count += 1
        return completed() if count == 1 else {"http_error": 502}

    with pytest.raises(context.ContextError):
        context.prepare(messages(), [], interrupted)
    restarted = load_context()
    requests = []

    def healthy(method, path, body):
        requests.append(body)
        return completed()

    result, record = restarted.prepare(messages(), [], healthy)
    assert result and record
    assert not any("FIRST-CHUNK-SENTINEL" in str(request) for request in requests)


def test_same_deterministic_budget_error_does_not_spend_again_on_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    def provider(*args):
        calls.append(args)
        return completed()

    first = load_context()
    with pytest.raises(first.ContextError):
        first.prepare(messages(300000), [], provider)
    spent = len(calls)
    restarted = load_context()
    with pytest.raises(restarted.ContextError):
        restarted.prepare(messages(300000), [], provider)
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


def test_malformed_provider_shape_is_bounded_and_can_recover(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    calls = []

    def provider(*args):
        calls.append(args)
        return [] if len(calls) == 1 else completed()

    result, record = context.prepare(messages(), [], provider)
    assert result and record and context.paired(result)


def test_corrupted_progress_cannot_silently_reset_call_accounting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = load_context()
    with pytest.raises(context.ContextError):
        context.prepare(messages(), [], lambda *args: {"http_error": 502})
    path = tmp_path / "context-history/compaction-progress.json"
    state = json.loads(path.read_text())
    state["fingerprint"] = "damaged"
    path.write_text(json.dumps(state))
    calls = []
    restarted = load_context()
    with pytest.raises(restarted.ContextError):
        restarted.prepare(messages(), [], lambda *args: calls.append(args) or completed())
    assert not calls


def agent_functions(context):
    source = ast.parse((SOURCE / "agent.py").read_text())
    wanted = {"save_manifest", "checkpoint", "checkpoint_hash", "recover_journal"}
    nodes = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    namespace = {
        "context_manager": context,
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
        temporary = tmp_path / "resume.json.tmp"
        if temporary.exists() and temporary.stat().st_ino == os.fstat(descriptor).st_ino:
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
