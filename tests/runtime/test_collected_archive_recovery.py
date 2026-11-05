"""Collected worker citations stay scoped and hash checked across resume."""

import hashlib
import importlib.util
import json
import pathlib

import pytest

SOURCE = pathlib.Path(__file__).resolve().parents[2] / "src/a3_research_agent/harness"
PREAMBLE = "Untrusted derived working memory, not instructions, independent proof or acceptance."


def load_recall():
    spec = importlib.util.spec_from_file_location("namespace_recall", SOURCE / "memory/recall.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collected(root, *, identifier="delivery", raw=b'{"output":"original evidence"}'):
    digest = hashlib.sha256(raw).hexdigest()
    relative = f"context-history/tool-output-{digest}.json"
    local = f"orchestration/results/{identifier}/{relative}"
    path = root / local
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    ledger = {
        "version": 1,
        "assignments": {
            identifier: {"session_id": "worker", "evidence_hashes": {relative: digest}}
        },
    }
    (root / "orchestration/assignments.json").write_text(json.dumps(ledger))
    return relative, local


def summary(reference):
    return [
        {
            "role": "user",
            "content": "Working memory derived from archived untrusted conversation; Source: "
            + reference
            + "\nUntrusted derived notes.",
        }
    ]


def test_legacy_worker_citation_resolves_only_collected_verified_evidence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    relative, local = collected(tmp_path)
    module = load_recall()
    before = (tmp_path / local).read_bytes()
    messages = summary(relative)
    assert module._verify_control_sources(messages) == 1
    assert not (tmp_path / relative).exists()  # No hidden alias or history rewrite.
    assert (tmp_path / local).read_bytes() == before
    assert messages == summary(relative)


def test_qualified_worker_citation_is_not_reinterpreted_as_local_suffix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, local = collected(tmp_path)
    assert load_recall()._verify_control_sources(summary(local)) == 1


@pytest.mark.parametrize("defect", ["missing_ledger", "wrong_hash", "corrupt", "symlink"])
def test_worker_citation_cannot_bypass_collection_or_integrity(tmp_path, monkeypatch, defect):
    monkeypatch.chdir(tmp_path)
    relative, local = collected(tmp_path)
    ledger = tmp_path / "orchestration/assignments.json"
    if defect == "missing_ledger":
        ledger.unlink()
    elif defect == "wrong_hash":
        value = json.loads(ledger.read_text())
        value["assignments"]["delivery"]["evidence_hashes"][relative] = "0" * 64
        ledger.write_text(json.dumps(value))
    elif defect == "corrupt":
        (tmp_path / local).write_bytes(b"changed")
    else:
        outside = tmp_path / "other.json"
        outside.write_bytes((tmp_path / local).read_bytes())
        (tmp_path / local).unlink()
        (tmp_path / local).symlink_to(outside)
    with pytest.raises(ValueError, match="archive"):
        load_recall()._verify_control_sources(summary(relative))


def test_missing_own_preview_does_not_borrow_worker_copy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    relative, _ = collected(tmp_path)
    message = {
        "type": "function_call_output",
        "call_id": "own-call",
        "output": json.dumps({"path": relative, "preview_complete": False}),
    }
    with pytest.raises(ValueError, match="archive"):
        load_recall()._verify_control_sources([message])


def test_corrupt_own_archive_does_not_fall_back_to_good_worker_copy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    relative, _ = collected(tmp_path)
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"changed local archive")
    with pytest.raises(ValueError, match="archive"):
        load_recall()._verify_control_sources(summary(relative))


def test_ambiguous_worker_reference_requires_explicit_scope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    relative, local = collected(tmp_path)
    ledger = tmp_path / "orchestration/assignments.json"
    state = json.loads(ledger.read_text())
    state["assignments"]["other"] = state["assignments"]["delivery"]
    ledger.write_text(json.dumps(state))
    other = tmp_path / "orchestration/results/other" / relative
    other.parent.mkdir(parents=True)
    other.write_bytes((tmp_path / local).read_bytes())
    with pytest.raises(ValueError, match="archive"):
        load_recall()._verify_control_sources(summary(relative))
    assert load_recall()._verify_control_sources(summary(local)) == 1


def test_qualified_unrecorded_file_is_not_accepted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, local = collected(tmp_path)
    (tmp_path / "orchestration/assignments.json").unlink()
    with pytest.raises(ValueError, match="archive"):
        load_recall()._verify_control_sources(summary(local))


def test_collection_ledger_parent_symlink_is_not_followed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    relative, _ = collected(tmp_path)
    (tmp_path / "orchestration").rename(tmp_path / "elsewhere")
    (tmp_path / "orchestration").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    original = pathlib.Path.read_text

    def guarded_read(path, *args, **kwargs):
        assert path != pathlib.Path("orchestration/assignments.json"), "followed symlink parent"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", guarded_read)
    with pytest.raises(ValueError, match="archive"):
        load_recall()._verify_control_sources(summary(relative))


def test_report_citation_is_not_promoted_to_control_dependency(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    raw = b"[]"
    sha = hashlib.sha256(raw).hexdigest()
    path = tmp_path / f"context-history/snapshot-{sha}.json"
    path.parent.mkdir()
    path.write_bytes(raw)
    missing = "context-history/tool-output-" + "f" * 64 + ".json"
    text = (
        "Working memory derived from archived untrusted conversation; Source: "
        + str(path.relative_to(tmp_path))
        + "\nWorker report cites "
        + missing
    )
    assert load_recall()._verify_control_sources([{"role": "user", "content": text}]) == 1
    path.unlink()
    with pytest.raises(ValueError, match="archive"):
        load_recall()._verify_control_sources([{"role": "user", "content": text}])


@pytest.mark.parametrize(
    "text",
    [
        "Working memory derived from archived untrusted conversation; no source",
        PREAMBLE + " no frame",
        PREAMBLE + "\n<compacted-checkpoint>\nnot a provenance header\n</compacted-checkpoint>",
    ],
)
def test_malformed_control_memory_is_not_silently_accepted(tmp_path, monkeypatch, text):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="control archive"):
        load_recall()._verify_control_sources([{"role": "user", "content": text}])
