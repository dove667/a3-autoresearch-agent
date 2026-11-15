"""Controlled research outcomes live outside the freely writable workspace."""

import json

from a3_research_agent.harness import research_status
from a3_research_agent.host.runs.supervisor import evaluate_outcome


def test_outcome_tool_writes_state_and_host_reads_workspace_evidence(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("A3_WORKSPACE", str(workspace))
    evidence = workspace / "auth-error.txt"
    evidence.write_text("Controlled expired authentication")
    blocker = {
        "kind": "authentication",
        "requires_human": True,
        "reason": "Authentication expired",
        "required_action": "Renew access",
        "evidence": ["auth-error.txt"],
    }
    result = research_status.update(
        {
            "action": "outcome",
            "status": "blocked",
            "reason": "Needs access",
            "external_blocker": blocker,
        }
    )
    assert result["status"] == "blocked"
    state_root = tmp_path / "conversation"
    assert not (workspace / "run-outcome.json").exists()
    assert evaluate_outcome(workspace, state_root)["external_blocker"] == blocker
    before = (state_root / "run-outcome.json").read_bytes()
    blocker["evidence"] = ["../outside.txt"]
    assert "error" in research_status.update(
        {
            "action": "outcome",
            "status": "blocked",
            "reason": "Needs access",
            "external_blocker": blocker,
        }
    )
    assert (state_root / "run-outcome.json").read_bytes() == before
    assert "error" in research_status.update({"action": "outcome", "status": "complete"})
    assert (state_root / "run-outcome.json").read_bytes() == before


def test_progress_routes_outcomes_and_ignores_workspace_metadata(tmp_path, monkeypatch):
    from a3_research_agent.harness import agent

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("A3_WORKSPACE", str(workspace))
    (workspace / "run-outcome.json").write_text('{"status":"blocked"}')
    result = agent.tool(
        "progress", {"action": "outcome", "status": "continue", "reason": "Saved progress"}
    )
    assert result["status"] == "continue"
    state_root = tmp_path / "conversation"
    assert evaluate_outcome(workspace, state_root) == {
        "status": "continue",
        "reason": "Saved progress",
    }
    assert json.loads((workspace / "run-outcome.json").read_text())["status"] == "blocked"
    spec = next(tool for tool in agent.tools if tool["name"] == "progress")
    assert "outcome" in spec["parameters"]["properties"]["action"]["enum"]
