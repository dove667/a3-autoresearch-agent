"""Published research skills remain complete, frozen and discoverable."""

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from a3_research_agent.harness import input_skills, skill_registry
from a3_research_agent.host.inputs.versions import InputVersions

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "skills/a3-mujoco-autoresearch"
PACKAGES = (
    "a3-mujoco-autoresearch",
    "a3-policy-integration",
    "a3-job-recovery",
    "a3-frame-cache",
    "a3-training-progress",
    "a3-model-provenance",
)


def task_directory(directory):
    directory.mkdir()
    for name, content in {
        "goal.md": "Research this question",
        "acceptance.md": "## result. Preserve evidence",
        "protocol.md": "Record conditions and results",
    }.items():
        (directory / name).write_text("Version: 1.0.0\n\n" + content + "\n")
    return directory


@pytest.mark.parametrize("identifier", PACKAGES)
def test_research_catalog_maps_to_readable_versioned_reference(identifier):
    package = ROOT / "skills" / identifier
    entry = json.loads((package / "assets/catalog-entry.json").read_text())
    body = (ROOT / entry["local_source"]).read_text()
    frontmatter = yaml.safe_load(body.split("---", 2)[1])
    assert entry["id"] == frontmatter["name"] == package.name
    assert entry["version"] == frontmatter["metadata"]["version"]
    assert entry["status"] == "reference"
    for target in re.findall(r"\]\(([^)]+)\)", body):
        if "://" not in target:
            assert (package / target.partition("#")[0]).is_file(), target


def test_all_reviewed_skills_install_and_agent_edits_survive_resume(tmp_path, monkeypatch):
    inputs = InputVersions(ROOT, tmp_path / "run", custom_dir=task_directory(tmp_path / "task"))
    inputs.activate(inputs.prepare(), "start")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    input_skills.install_input_skills(inputs.run / "inputs")
    listing = skill_registry.dispatch({"action": "list"})
    assert {entry["id"] for entry in listing["skills"]} == set(PACKAGES)
    for identifier in PACKAGES:
        content, offset = "", 0
        while True:
            entry = skill_registry.dispatch(
                {"action": "read", "id": identifier, "limit": 8000, "offset": offset}
            )
            content += entry["content"]
            if entry["eof"]:
                break
            offset = entry["end"]
        assert content == (ROOT / "skills" / identifier / "SKILL.md").read_text()
    original = skill_registry.dispatch({"action": "read", "id": "a3-job-recovery"})
    saved = skill_registry.dispatch(
        {
            "action": "save",
            "id": "a3-job-recovery",
            "description": "Local repair lesson",
            "content": "Keep the original job ID until its outcome is reconciled.",
            "source": "Local diagnostic fixture",
            "expected_sha256": original["sha256"],
        }
    )
    assert "error" not in saved
    input_skills.install_input_skills(inputs.run / "inputs")
    assert (
        skill_registry.dispatch({"action": "read", "id": "a3-job-recovery"})["sha256"]
        == saved["sha256"]
    )


def test_deployed_skill_is_frozen_discoverable_and_not_injected(tmp_path, monkeypatch):
    source = task_directory(tmp_path / "task")
    inputs = InputVersions(ROOT, tmp_path / "run", custom_dir=source)
    proposal = inputs.prepare()
    inputs.activate(proposal, "start")
    relative = "skills/a3-mujoco-autoresearch/SKILL.md"
    assert inputs.contents[relative] == (ROOT / relative).read_text()
    assert inputs.contents[relative] not in inputs.model_instructions()[1]
    shutil.rmtree(source)
    restored = InputVersions(ROOT, inputs.run)
    assert restored.prepare()["contents"][relative] == proposal["contents"][relative]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    input_skills.install_input_skills(inputs.run / "inputs")
    listing = skill_registry.dispatch({"action": "list", "query": "mujoco research"})
    assert "a3-mujoco-autoresearch" in {item["id"] for item in listing["skills"]}
    assert (
        skill_registry.dispatch({"action": "read", "id": "a3-mujoco-autoresearch", "limit": 8000})[
            "sha256"
        ]
        == inputs.manifest["files"][relative]["sha256"]
    )
