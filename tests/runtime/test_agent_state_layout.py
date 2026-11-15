"""Machine state stays together while research files retain their public location."""

import hashlib
import json

import pytest

from a3_research_agent.harness import agent, history, operational_state, state_paths
from a3_research_agent.harness.context.manager import atomic
from a3_research_agent.host.sessions.packages import read_continuation


def test_state_store_requires_configuration_but_host_can_supply_an_explicit_root(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("A3_STATE_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="Host must configure"):
        state_paths.state_root()
    root = tmp_path / "conversation"
    assert state_paths.path("checkpoint.json", root) == root / "checkpoint.json"


def test_runtime_state_and_handoff_use_the_internal_directory(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    research = workspace / "research"
    research.mkdir()
    (research / "finding.md").write_text("Retain the observed result")
    atomic(state_paths.path(state_paths.CHECKPOINT), "[]")
    agent.checkpoint([])
    agent.log({"event": "state-saved"})
    history.append_history({"role": "user", "content": "Original observation"})
    operational_state.record_gateway("POST", "/jobs", {"job_id": "a" * 32, "status": "queued"})
    assert state_paths.path(state_paths.TRAJECTORY).is_file()
    assert state_paths.path(state_paths.HISTORY).is_file()
    assert state_paths.path(state_paths.OPERATIONS).is_file()
    assert json.loads(state_paths.path(state_paths.CHECKPOINT).read_text()) == []
    assert {path.name for path in workspace.iterdir()} == {
        "research",
    }
    continuation = read_continuation(workspace, state_paths.state_root())
    assert state_paths.CHECKPOINT not in continuation["files"]
    assert "research/finding.md" in continuation["files"]


def test_logical_archive_sources_restore_from_state_without_rewriting_bytes(tmp_path, monkeypatch):
    from a3_research_agent.harness.context import manager

    monkeypatch.setattr(manager, "TOKEN_CONFIG", {"feedback_refs": []})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    reference = manager.archive([{"role": "user", "content": "Original observation"}], "snapshot")
    source = state_paths.path(reference)
    original = source.read_bytes()
    assert hashlib.sha256(original).hexdigest() in source.name
    derived = manager.memory_message("Preserve the observation", reference)
    history.ensure_history([derived])
    assert history.load_history() == [derived]
    assert source.read_bytes() == original
    assert reference.startswith("context-history/")
    assert not list(workspace.iterdir())


def test_plain_file_tools_can_read_but_cannot_mutate_state(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("A3_WORKSPACE", str(workspace))
    monkeypatch.setenv("A3_INPUT_ROOT", str(tmp_path / "inputs"))
    monkeypatch.setenv("A3_REFERENCE_ROOT", str(tmp_path / "reference"))
    atomic(state_paths.path(state_paths.CHECKPOINT), "[]")
    checkpoint = state_paths.path(state_paths.CHECKPOINT)
    assert agent.tool("read", {"path": str(checkpoint)})["content"] == "[]"
    assert agent.tool("write", {"path": str(checkpoint), "content": "overwritten"}) == "read only"
    result = agent.tool("edit", {"path": str(checkpoint), "old_text": "[]", "new_text": "{}"})
    assert result["error"] == "path_not_accessible"
    assert checkpoint.read_text() == "[]"
