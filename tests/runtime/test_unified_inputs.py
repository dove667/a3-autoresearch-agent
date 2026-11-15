"""Task selection is explicit and independent of historical stage prompts."""

from pathlib import Path

import pytest
from task_input_support import task_inputs

from a3_research_agent.host.inputs.versions import InputVersions

ROOT = Path(__file__).resolve().parents[2]


def test_task_inputs_add_only_current_shared_runtime_guidance(tmp_path):
    directory = task_inputs(tmp_path / "task")
    run = tmp_path / "run"
    versions = InputVersions(ROOT, run, custom_dir=directory)
    proposal = versions.prepare()
    assert {"SOUL.md", "execution.md", "workspace-instructions.md"} <= proposal["contents"].keys()
    assert not any(
        "phase-" in name or name in {"bootstrap.yaml", "readiness-task.md"}
        for name in proposal["contents"]
    )
    manifest = versions.activate(proposal, "start")
    assert "phase" not in manifest and "task_profile" not in manifest
    assert InputVersions(ROOT, run).prepare()["digest"] == proposal["digest"]
    assert versions.acceptance_checks() == ("result",)


@pytest.mark.parametrize("missing", ["goal.md", "acceptance.md", "protocol.md"])
def test_new_task_requires_complete_explicit_inputs(tmp_path, missing):
    directory = task_inputs(tmp_path / "task")
    (directory / missing).unlink()
    with pytest.raises(ValueError, match="missing"):
        InputVersions(ROOT, None, custom_dir=directory)


def test_new_task_has_no_implicit_historical_goal():
    with pytest.raises(ValueError, match="explicit"):
        InputVersions(ROOT, None)
