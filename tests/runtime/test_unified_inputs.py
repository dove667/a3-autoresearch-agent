"""Continuous research uses the current prompts independently of old stage files."""

from pathlib import Path

from input_source_support import deployment_source

from a3_research_agent.host.inputs.versions import InputVersions

ROOT = Path(__file__).resolve().parents[2]


def test_current_inputs_have_no_phase_or_task_selection(tmp_path):
    run = tmp_path / "run"
    versions = InputVersions(ROOT, run)
    proposal = versions.prepare()
    assert {"SOUL.md", "execution.md", "workspace-instructions.md"} <= proposal["contents"].keys()
    assert not any(
        "phase-" in name
        or name
        in {"bootstrap.yaml", "readiness-task.md", "goal.md", "acceptance.md", "protocol.md"}
        for name in proposal["contents"]
    )
    manifest = versions.activate(proposal, "start")
    assert not {"phase", "task_profile", "acceptance_checks"} & manifest.keys()
    assert InputVersions(ROOT, run).prepare()["digest"] == proposal["digest"]


def test_new_research_needs_only_three_prompts_and_complete_skills(tmp_path):
    root = deployment_source(tmp_path / "source")
    contents = InputVersions(root, None).prepare()["contents"]
    assert {"SOUL.md", "execution.md", "workspace-instructions.md"} <= contents.keys()
    assert all(
        name.startswith("skills/")
        for name in contents.keys() - {"SOUL.md", "execution.md", "workspace-instructions.md"}
    )
