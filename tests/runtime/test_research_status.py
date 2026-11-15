"""Whole-run reports are separate from the research task plan."""

import json

from a3_research_agent.harness import control_tools, paths, research_status, state_paths


def test_continue_status_is_flat_and_does_not_create_task_records():
    result = control_tools.dispatch(
        "research_status",
        {"status": "continue", "reason": "Results recorded; further evaluation planned"},
        None,
    )
    assert result["status"] == "continue"
    assert "external_blocker" not in result
    assert not paths.plan_file().exists()
    assert json.loads(state_paths.path(state_paths.OUTCOME).read_text())["status"] == "continue"


def test_blocked_status_requires_real_evidence_and_infers_operator_requirement(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("A3_WORKSPACE", str(workspace))
    args = {
        "status": "blocked",
        "reason": "Dataset export is unavailable",
        "kind": "missing_resource",
        "required_action": "Restore the referenced dataset export",
        "evidence": ["experiments/error.txt"],
    }
    assert "error" in research_status.update(args)
    (workspace / "experiments").mkdir()
    (workspace / "experiments/error.txt").write_text("export request returned not found")
    result = research_status.update(args)
    assert result["external_blocker"]["requires_human"] is True
    assert result["external_blocker"]["evidence"] == ["experiments/error.txt"]
    assert research_status.read_outcome(workspace, state_paths.state_root())["status"] == "blocked"


def test_obsolete_progress_envelope_and_unrelated_blocker_fields_are_rejected():
    assert "error" in research_status.update(
        {"action": "outcome", "status": "continue", "reason": "old envelope"}
    )
    assert "error" in research_status.update(
        {"status": "continue", "reason": "working", "kind": "permission"}
    )
