"""Machine state stays together while research files retain their public location."""

import json

from a3_research_agent.harness import agent, history, operational_state, state_paths
from a3_research_agent.harness.context.manager import atomic
from a3_research_agent.host.sessions.packages import read_continuation


def test_runtime_state_and_handoff_use_the_internal_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    research = tmp_path / "research"
    research.mkdir()
    (research / "finding.md").write_text("Retain the observed result")
    atomic(state_paths.CHECKPOINT, "[]")
    agent.checkpoint([])
    agent.log({"event": "state-saved"})
    history.append_history({"role": "user", "content": "Original observation"})
    operational_state.record_gateway("POST", "/jobs", {"job_id": "a" * 32, "status": "queued"})
    assert (tmp_path / state_paths.TRAJECTORY).is_file()
    assert (tmp_path / state_paths.HISTORY).is_file()
    assert (tmp_path / state_paths.OPERATIONS).is_file()
    assert json.loads((tmp_path / state_paths.CHECKPOINT).read_text()) == []
    assert {path.name for path in tmp_path.iterdir()} == {
        ".agent",
        "research",
        "resume.json",
        "continuation.json",
    }
    continuation = read_continuation(tmp_path)
    assert state_paths.CHECKPOINT in continuation["files"]
    assert "research/finding.md" in continuation["files"]
