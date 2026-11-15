"""Prompts freeze independently while the project skill tree remains live."""

import shutil
from pathlib import Path

from input_source_support import deployment_source

from a3_research_agent.harness import skill_registry
from a3_research_agent.host.inputs.versions import InputVersions

ROOT = Path(__file__).resolve().parents[2]


def test_skill_changes_do_not_enter_input_snapshots_or_change_the_prompt_revision(tmp_path):
    source = deployment_source(tmp_path / "source")
    root = tmp_path / "skills"
    shutil.copytree(ROOT / "skills", root)
    inputs = InputVersions(source, tmp_path / "run")
    first = inputs.activate(inputs.prepare(), "start")
    assert set(first["files"]) == {"SOUL.md", "execution.md", "workspace-instructions.md"}
    before = skill_registry.dispatch({"action": "read", "id": "a3-job-recovery"}, root=root)
    changed = skill_registry.dispatch(
        {
            "action": "save",
            "id": "a3-job-recovery",
            "description": "Updated recovery method",
            "content": "# Updated\nReconcile the existing job ID.",
            "source": "fixture/trial",
            "expected_sha256": before["sha256"],
        },
        root=root,
    )
    assert "error" not in changed
    assert InputVersions(source, inputs.run).prepare()["digest"] == first["digest"]
    assert InputVersions(source, inputs.run).prepare_update()["digest"] == first["digest"]
    assert (
        skill_registry.dispatch({"action": "read", "id": "a3-job-recovery"}, root=root)["sha256"]
        == changed["sha256"]
    )
    assert not (inputs.run / "inputs/skills").exists()


def test_resume_reads_saved_prompts_without_any_skill_directory(tmp_path):
    source = deployment_source(tmp_path / "source")
    inputs = InputVersions(source, tmp_path / "run")
    first = inputs.activate(inputs.prepare(), "start")
    shutil.rmtree(source / "skills")
    assert InputVersions(source, inputs.run).prepare()["digest"] == first["digest"]
    assert set(InputVersions(source, None).prepare()["files"]) == set(first["files"])


def test_harness_payload_uses_the_live_library_and_has_no_installation_module():
    from a3_research_agent.harness_payload import build_harness_payload

    sources = build_harness_payload()
    assert "a3_research_agent/harness/skill_library.py" in sources
    assert not any("input_skills" in name for name in sources)
