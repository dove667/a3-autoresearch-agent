# ruff: noqa: S102
# AST executes only selected local definitions; never the Agent startup or model loop.
"""Lossless bounded results, including the live failure's asynchronous tail shape."""

import ast
import base64
import hashlib
import importlib.util
import json
import pathlib
import threading
import types

import pytest

SOURCE = pathlib.Path(__file__).resolve().parents[2] / "src/a3_research_agent/harness"


def load(name):
    spec = importlib.util.spec_from_file_location(
        "boundary_" + name,
        SOURCE
        / (
            {
                "context_manager": "context/manager.py",
                "memory_state": "memory/state.py",
                "spill": "context/spill.py",
                "recall": "memory/recall.py",
            }[name]
        ),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def output(result, call_id="call-test"):
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(result, ensure_ascii=False),
    }


@pytest.mark.parametrize(
    "result",
    [
        {"nested": ["a" * 100000]},
        ["b" * 100000],
        "\0" * 49000,
        '\\"' * 49000,
        "中文🙂" * 20000,
    ],
)
def test_ingress_bounds_serialized_envelope_and_retains_exact_result(tmp_path, monkeypatch, result):
    monkeypatch.chdir(tmp_path)
    cm = load("context_manager")
    call = {"call_id": "call-test", "name": "gateway", "arguments": "{}"}
    bounded = cm.bound_tool_result(result, call)
    message = output(bounded)
    assert cm.size(message) <= 12000
    view = json.loads(message["output"])
    assert view["preview_complete"] is False
    original = json.loads(pathlib.Path(view["path"]).read_text())
    assert original == output(result)
    assert hashlib.sha256(pathlib.Path(view["path"]).read_bytes()).hexdigest() in view["path"]


@pytest.mark.parametrize("result", [None, False, 12, 1.5, "small", {"status": "completed"}])
def test_small_results_are_not_reinterpreted(tmp_path, monkeypatch, result):
    monkeypatch.chdir(tmp_path)
    assert load("context_manager").bound_tool_result(result, {"call_id": "c"}) == result


@pytest.mark.parametrize(
    "raw,encoding",
    [("中文\n".encode() * 20000, "utf-8"), (b"\x89PNG\xff\0" * 20000, "binary"), (b"", "utf-8")],
)
def test_base64_artifact_has_verified_local_representation(tmp_path, monkeypatch, raw, encoding):
    monkeypatch.chdir(tmp_path)
    cm = load("context_manager")
    result = {
        "path": "../../untrusted-name",
        "encoding": "base64",
        "data": base64.b64encode(raw).decode(),
    }
    # Explicit archival also covers empty artifacts without forcing all small results to spill.
    message = cm.archived_output(output(result), 6000)
    view = json.loads(message["output"])
    artifact = view["artifact"]
    assert pathlib.Path(artifact["path"]).read_bytes() == raw
    assert artifact["sha256"] == hashlib.sha256(raw).hexdigest()
    assert artifact["encoding"] == encoding
    assert pathlib.Path(artifact["path"]).parent == pathlib.Path("context-history")
    assert "base64" not in view.get("preview", "")
    assert cm.size(message) <= 6000


def test_reducing_an_archived_preview_does_not_archive_the_pointer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cm = load("context_manager")
    original = output({"text": "x" * 100000})
    first = cm.archived_output(original, 6000)
    second = cm.archived_output(first, 2500)
    assert json.loads(first["output"])["path"] == json.loads(second["output"])["path"]
    assert json.loads(pathlib.Path(json.loads(second["output"])["path"]).read_text()) == original
    assert cm.size(second) <= 2500


def test_aggregate_batch_fits_without_dropping_calls_or_results(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cm = load("context_manager")
    messages = [{"role": "user", "content": "Current correction and pending work"}]
    for n in range(12):
        messages.append(
            {"type": "function_call", "call_id": str(n), "name": "read", "arguments": "{}"}
        )
    messages.extend(output({"text": "x" * 6000}, str(n)) for n in range(12))
    before = json.loads(json.dumps(messages))
    fitted = cm.fit_tool_outputs(messages, 24000)
    assert cm.message_cost(fitted) <= 24000 and cm.paired(fitted)
    assert [x.get("call_id") for x in fitted] == [x.get("call_id") for x in messages]
    assert messages == before
    assert fitted[0] == messages[0]


def test_many_moderate_outputs_use_the_actual_metadata_floor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cm = load("context_manager")
    messages = [
        {"type": "function_call", "call_id": str(i), "name": "gateway", "arguments": "{}"}
        for i in range(10)
    ] + [output("x" * 1000, str(i)) for i in range(10)]
    fitted = cm.fit_tool_outputs(messages, 10000)
    assert cm.paired(fitted) and cm.message_cost(fitted) <= 10000


def test_atomic_concurrent_writers_publish_only_complete_values(tmp_path, monkeypatch):
    cm = load("context_manager")
    target = tmp_path / "state.json"
    barrier = threading.Barrier(2)
    replace = cm.os.replace
    errors = []

    def synchronized_replace(*args):
        barrier.wait(3)
        return replace(*args)

    monkeypatch.setattr(cm.os, "replace", synchronized_replace)

    def writer(value):
        try:
            cm.atomic(target, value)
        except Exception as e:  # noqa: BLE001 - collect every background writer failure
            errors.append(type(e).__name__)

    threads = [threading.Thread(target=writer, args=(c * 5000,)) for c in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not errors
    assert target.read_text() in {"a" * 5000, "b" * 5000}


def test_corrupt_legacy_spill_is_not_reused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spill = load("spill")
    first = json.loads(spill.maybe_spill("x" * 60000))
    pathlib.Path(first["path"]).write_text("TRUNCATED")
    with pytest.raises(Exception, match="corrupt|verification"):
        spill.maybe_spill("x" * 60000)


def agent_functions(*names):
    tree = ast.parse((SOURCE / "agent.py").read_text())
    return ast.Module(
        body=[
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names
        ],
        type_ignores=[],
    )


def test_execution_is_separate_from_result_publication(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []
    raw = "x" * 100000
    cm = load("context_manager")
    ns = {
        "json": json,
        "tool": lambda *args: calls.append(args) or raw,
        "spill_tools": types.SimpleNamespace(
            maybe_spill=lambda *a, **k: (_ for _ in ()).throw(OSError())
        ),
        "context_manager": cm,
        "memory_state": types.SimpleNamespace(record_page=lambda *a: None),
    }
    exec(compile(agent_functions("dispatch_tool_call"), "isolated-agent", "exec"), ns)
    assert ns["dispatch_tool_call"]({"call_id": "c", "name": "gateway", "arguments": "{}"}) == raw
    assert len(calls) == 1


def test_archive_failure_leaves_executed_raw_result_in_journal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cm = load("context_manager")
    ns = {
        "json": json,
        "context_manager": cm,
        "memory_state": types.SimpleNamespace(record_page=lambda *a: None),
        "log": lambda *a: None,
        "recall_tools": types.SimpleNamespace(append_history=lambda *a: None),
    }
    exec(
        compile(
            agent_functions("ToolResultPersistenceError", "save_tool_result"),
            "isolated-agent",
            "exec",
        ),
        ns,
    )
    monkeypatch.setattr(
        cm, "bound_tool_result", lambda *a, **k: (_ for _ in ()).throw(OSError("disk failure"))
    )
    raw = {"job_id": "j", "status": "created", "data": "x" * 100000}
    journal = {"results": {}}
    messages = []
    with pytest.raises(ns["ToolResultPersistenceError"]):
        ns["save_tool_result"]({"call_id": "c", "name": "gateway"}, raw, journal, messages, 0)
    assert json.loads(pathlib.Path("pending-calls.json").read_text())["results"]["c"] == raw
    assert not messages


def test_recovery_publishes_persisted_result_without_reexecuting_tool(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cm = load("context_manager")
    base = [{"role": "user", "content": "original task"}]
    cm.atomic("checkpoint.json", json.dumps(base))
    call = {"type": "function_call", "call_id": "c", "name": "gateway", "arguments": "{}"}
    raw = {"job_id": "already-created", "status": "completed", "data": "x" * 100000}
    journal = {
        "base_hash": hashlib.sha256(pathlib.Path("checkpoint.json").read_bytes()).hexdigest(),
        "output": [call],
        "results": {"c": raw},
    }
    cm.atomic("pending-calls.json", json.dumps(journal))
    ns = {
        "json": json,
        "pathlib": pathlib,
        "hashlib": hashlib,
        "context_manager": cm,
        "save_manifest": lambda: None,
        "checkpoint": lambda value: cm.atomic("checkpoint.json", json.dumps(value)),
    }
    exec(
        compile(agent_functions("checkpoint_hash", "recover_journal"), "isolated-recovery", "exec"),
        ns,
    )
    restored = ns["recover_journal"](base)
    view = json.loads(restored[-1]["output"])
    original = json.loads(pathlib.Path(view["path"]).read_text())
    assert json.loads(original["output"]) == raw
    assert cm.paired(restored) and restored[-1]["call_id"] == "c"
    assert json.loads(pathlib.Path("pending-calls.json").read_text()) is None
    assert ns["recover_journal"](restored) == restored


def test_invalid_base64_is_preserved_as_an_explicit_decode_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cm = load("context_manager")
    original = output({"encoding": "base64", "data": "%bad%", "path": "metadata.json"})
    view = json.loads(cm.archived_output(original, 4000)["output"])
    assert view["artifact"]["error"] == "invalid_base64"
    assert json.loads(pathlib.Path(view["path"]).read_text()) == original


def test_corrupt_decoded_artifact_does_not_get_a_success_reference(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cm = load("context_manager")
    original = output({"encoding": "base64", "data": base64.b64encode(b"facts").decode()})
    view = json.loads(cm.archived_output(original, 4000)["output"])
    pathlib.Path(view["artifact"]["path"]).write_text("changed")
    with pytest.raises(cm.ContextError, match="verification"):
        cm.archived_output(original, 4000)


def test_irreducible_envelope_is_a_bounded_failure_not_lost_calls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cm = load("context_manager")
    messages = [
        {
            "type": "function_call",
            "call_id": "c",
            "name": "gateway",
            "arguments": "argument " * 4000,
        },
        output({"status": "completed"}, "c"),
    ]
    original = json.loads(json.dumps(messages))
    with pytest.raises(cm.ContextError, match="Irreducible"):
        cm.fit_tool_outputs(messages, 1000)
    assert messages == original and cm.paired(messages)


def test_binary_read_reports_type_without_generic_execution_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = load("memory_state")
    raw = b"\x89PNG\xff\0" * 20
    pathlib.Path("image.bin").write_bytes(raw)
    page = state.read_page("image.bin")
    assert page["encoding"] == "binary" and page["bytes"] == len(raw)
    assert page["sha256"] == hashlib.sha256(raw).hexdigest()


def test_read_receipt_recovery_is_idempotent_and_can_mark_delivery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = load("memory_state")
    page = {
        "path": "facts.txt",
        "sha256": "a" * 64,
        "offset": 0,
        "end": 4,
        "total": 4,
        "content": "fact",
    }
    state.record_page("call", page)
    state.record_page("call", dict(page))
    state.mark_delivered([output(page, "call")])
    before = state.load()
    state.record_page("call", dict(page))
    after = state.load()
    assert before == after and after["counters"]["read_calls"] == 1
    assert next(iter(after["reads"].values()))["delivered"] == [[0, 4]]


def test_raw_read_journal_rebuilds_receipt_before_model_delivery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cm, state = load("context_manager"), load("memory_state")
    base = [{"role": "user", "content": "task"}]
    cm.atomic("checkpoint.json", json.dumps(base))
    page = {
        "path": "facts.txt",
        "sha256": "a" * 64,
        "offset": 0,
        "end": 4,
        "total": 4,
        "content": "fact",
    }
    call = {"type": "function_call", "call_id": "c", "name": "read", "arguments": "{}"}
    cm.atomic(
        "pending-calls.json",
        json.dumps(
            {
                "base_hash": hashlib.sha256(
                    pathlib.Path("checkpoint.json").read_bytes()
                ).hexdigest(),
                "output": [call],
                "results": {"c": page},
            }
        ),
    )
    ns = {
        "json": json,
        "pathlib": pathlib,
        "hashlib": hashlib,
        "context_manager": cm,
        "memory_state": state,
        "save_manifest": lambda: None,
        "checkpoint": lambda value: cm.atomic("checkpoint.json", json.dumps(value)),
    }
    exec(
        compile(agent_functions("checkpoint_hash", "recover_journal"), "isolated-recovery", "exec"),
        ns,
    )
    restored = ns["recover_journal"](base)
    state.mark_delivered(restored)
    assert state.load()["counters"]["read_calls"] == 1
    assert next(iter(state.load()["reads"].values()))["delivered"] == [[0, 4]]


def test_decoded_artifact_recovery_regenerates_missing_derived_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cm = load("context_manager")
    message = cm.archived_output(
        output({"encoding": "base64", "data": base64.b64encode(b"facts").decode()}), 4000
    )
    view = json.loads(message["output"])
    path = pathlib.Path(view["artifact"]["path"])
    path.unlink()
    with pytest.raises(ValueError, match="archive"):
        load("recall")._verify_control_sources([message])
    cm.restore_artifact_views([message])
    assert path.read_bytes() == b"facts"
    assert load("recall")._verify_control_sources([message]) == 2
    path.write_bytes(b"corrupt")
    with pytest.raises(cm.ContextError, match="verification"):
        cm.restore_artifact_views([message])


def test_text_paging_streams_source_and_bounds_final_escaped_envelope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = load("memory_state")
    text = '中文🙂\0\\"' * 10000
    pathlib.Path("large.txt").write_text(text)
    monkeypatch.setattr(
        pathlib.Path,
        "read_bytes",
        lambda *a: (_ for _ in ()).throw(AssertionError("full-file read")),
    )
    page = state.read_page("large.txt", offset=11, limit=8000)
    assert page["content"] == text[page["offset"] : page["end"]]
    assert page["total"] == len(text) and page["end"] <= 8011
    page["receipt_id"] = "r" * 100
    assert load("context_manager").size(output(page)) <= 12000
