"""All project skills are directly discoverable and equally editable."""

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from a3_research_agent.harness import skill_registry

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (
    "a3-mujoco-autoresearch",
    "a3-policy-integration",
    "a3-job-recovery",
    "a3-frame-cache",
    "a3-training-progress",
    "a3-model-provenance",
)


@pytest.mark.parametrize("identifier", PACKAGES)
def test_research_catalog_maps_to_readable_versioned_reference(identifier):
    package = ROOT / "skills" / identifier
    entry = json.loads((package / "assets/catalog-entry.json").read_text())
    body = (ROOT / entry["local_source"]).read_text()
    frontmatter = yaml.safe_load(body.split("---", 2)[1])
    assert entry["id"] == frontmatter["name"] == package.name
    assert entry["version"] == frontmatter["metadata"]["version"]
    for target in re.findall(r"\]\(([^)]+)\)", body):
        if "://" not in target:
            assert (package / target.partition("#")[0]).is_file(), target


def test_every_existing_project_package_is_discoverable_and_editable(tmp_path, monkeypatch):
    library = tmp_path / "skills"
    shutil.copytree(ROOT / "skills", library)
    monkeypatch.setenv("A3_SKILLS_ROOT", str(library))
    monkeypatch.setenv("A3_SKILLS_HISTORY_ROOT", str(tmp_path / "history"))
    listing = skill_registry.dispatch({"action": "list"})
    assert {entry["id"] for entry in listing["skills"]} == set(PACKAGES)
    for identifier in PACKAGES:
        content, offset = "", 0
        while True:
            page = skill_registry.dispatch(
                {"action": "read", "id": identifier, "limit": 8000, "offset": offset}
            )
            assert "error" not in page, (identifier, page)
            content += page["content"]
            if page["eof"]:
                break
            offset = page["end"]
        assert content == (library / identifier / "SKILL.md").read_text()
        edited = skill_registry.dispatch(
            {
                "action": "save",
                "id": identifier,
                "description": "Reviewed method",
                "content": content + "\nFixture clarification.\n",
                "source": "Fixture research evidence",
                "expected_sha256": page["sha256"],
            }
        )
        assert "error" not in edited, (identifier, edited)
        assert edited["version"] != page["version"]
        assert (library / identifier / "SKILL.md").read_text().endswith("Fixture clarification.\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    assert skill_registry.dispatch({"action": "list"})["total"] == 6
    assert not (workspace / "skills").exists()


def test_resources_are_read_only_when_explicitly_requested(tmp_path):
    library = tmp_path / "skills"
    shutil.copytree(ROOT / "skills", library)
    files = skill_registry.dispatch(
        {"action": "list_resources", "id": "a3-mujoco-autoresearch"}, root=library
    )
    resource = "references/training-diagnostics.md"
    assert resource in {entry["path"] for entry in files["resources"]}
    assert "Training diagnostics" not in str(files)
    page = skill_registry.dispatch(
        {
            "action": "read_resource",
            "id": "a3-mujoco-autoresearch",
            "path": resource,
            "limit": 8000,
        },
        root=library,
    )
    assert page["content"] == (library / "a3-mujoco-autoresearch" / resource).read_text()[:8000]
