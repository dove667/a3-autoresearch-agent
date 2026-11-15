"""Preset and published inputs deliver a usable research skill reference tree."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from input_source_support import deployment_source

from a3_research_agent.cli.night_release import (
    night_state_directory,
    prepare_release,
    publish_release,
)
from a3_research_agent.harness import input_skills, skill_registry
from a3_research_agent.host.inputs.versions import PROMPT_SOURCES, InputVersions

ROOT = Path(__file__).resolve().parents[2]
SKILL = "skills/a3-mujoco-autoresearch"


def test_preset_snapshot_installs_research_references_without_prompt_injection(
    tmp_path, monkeypatch
):
    versions = InputVersions(ROOT, tmp_path / "run")
    manifest = versions.activate(versions.prepare(), "start")
    catalog_version = json.loads((ROOT / SKILL / "assets/catalog-entry.json").read_text())[
        "version"
    ]
    names = [
        f"{SKILL}/{name}"
        for name in (
            "SKILL.md",
            "assets/catalog-entry.json",
            "references/training-diagnostics.md",
            "scripts/compare_evaluations.py",
        )
    ]
    for name in names:
        assert (
            manifest["files"][name]["sha256"]
            == hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        )
        assert manifest["files"][name]["version"] == catalog_version
    assert versions.contents[f"{SKILL}/SKILL.md"] not in versions.model_instructions()[1]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    input_skills.install_input_skills(versions.run / "inputs")
    matches = skill_registry.dispatch({"action": "list", "query": "ACT training DAgger"})
    assert matches["skills"][0]["id"] == "a3-mujoco-autoresearch"
    entry = skill_registry.dispatch({"action": "read", "id": "a3-mujoco-autoresearch"})
    installed = workspace / entry["local_source"]
    assert installed.read_text() == versions.contents[f"{SKILL}/SKILL.md"]
    assert (installed.parent / "references/training-diagnostics.md").is_file()
    assert (installed.parent / "scripts/compare_evaluations.py").is_file()


def test_published_release_carries_the_same_research_skill_tree(tmp_path, monkeypatch):
    monkeypatch.setattr("a3_research_agent.cli.night_release.native_source", lambda *args: ROOT)
    handoff = tmp_path / "handoff.md"
    handoff.write_text("## 已核对结论\n\n已有实验已保存。\n\n## 待办\n\n继续研究。\n")
    anchor = tmp_path / "project/trajectories/run-original"
    current = InputVersions(ROOT, anchor)
    current.activate(current.prepare(), "start")
    proposal = prepare_release(ROOT, handoff, commit="a" * 40, run=anchor)
    assert f"{SKILL}/SKILL.md" in proposal["inputs"]
    assert f"{SKILL}/scripts/compare_evaluations.py" in proposal["inputs"]
    anchor = tmp_path / "project/trajectories/run-original"
    publish_release(anchor, proposal, proposal["digest"])
    inputs = night_state_directory(anchor) / "night-releases" / proposal["digest"] / "inputs"
    contents = {
        path.relative_to(inputs).as_posix(): path.read_text()
        for path in inputs.rglob("*")
        if path.is_file()
    }
    selected = InputVersions.from_proposal(
        ROOT,
        tmp_path / "new-run",
        {
            "contents": contents,
            "files": {name: {"source": str(inputs / name)} for name in contents},
        },
    )
    selected.activate(selected.prepare(), "start")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    input_skills.install_input_skills(selected.run / "inputs")
    assert skill_registry.dispatch({"action": "list"})["total"] == len(
        list((ROOT / "skills").glob("*/assets/catalog-entry.json"))
    )


def test_new_reviewed_package_is_discovered_without_a_runtime_allowlist(tmp_path):
    root = deployment_source(tmp_path / "repo")
    package = root / "skills/learned-method"
    (package / "assets").mkdir(parents=True)
    (package / "SKILL.md").write_text("# Reusable learned method\n")
    (package / "assets/catalog-entry.json").write_text(
        json.dumps(
            {
                "id": "learned-method",
                "version": "0.1.0",
                "status": "reference",
                "local_source": "skills/learned-method/SKILL.md",
                "description": "Learned method",
            }
        )
    )
    versions = InputVersions(root, tmp_path / "run")
    proposal = versions.prepare()
    assert proposal["contents"]["skills/learned-method/SKILL.md"] == "# Reusable learned method\n"
    assert proposal["files"]["skills/learned-method/SKILL.md"]["version"] == "0.1.0"


def test_incomplete_new_package_is_rejected_instead_of_silently_omitted(tmp_path):
    root = deployment_source(tmp_path / "repo")
    package = root / "skills/incomplete-method"
    package.mkdir()
    (package / "SKILL.md").write_text("# Unregistered method\n")
    with pytest.raises(ValueError, match="incomplete"):
        InputVersions(root, None)


@pytest.mark.parametrize("change", ["edit", "add", "remove"])
def test_skill_tree_change_requires_a_new_package_version(tmp_path, change):
    root = tmp_path / "repo"
    for relative in PROMPT_SOURCES.values():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    shutil.copytree(ROOT / SKILL, root / SKILL, ignore=shutil.ignore_patterns("__pycache__"))
    catalog_path = root / SKILL / "assets/catalog-entry.json"
    catalog = json.loads(catalog_path.read_text())
    catalog["version"] = "1.2.3"
    catalog_path.write_text(json.dumps(catalog))
    versions = InputVersions(root, tmp_path / "run")
    versions.activate(versions.prepare(), "start")
    reference = root / SKILL / "references/training-diagnostics.md"
    if change == "edit":
        reference.write_text(reference.read_text() + "\nA revised diagnostic.\n")
    elif change == "add":
        (reference.parent / "new-diagnostic.md").write_text("A new diagnostic.")
    else:
        reference.unlink()
    with pytest.raises(ValueError, match="version increment"):
        InputVersions(root, versions.run).prepare_update()
    catalog = json.loads(catalog_path.read_text())
    catalog["version"] = "1.2.4"
    catalog_path.write_text(json.dumps(catalog))
    upgraded = InputVersions(root, versions.run).prepare_update()
    assert upgraded["files"][f"{SKILL}/SKILL.md"]["version"] == "1.2.4"


def test_preset_skill_symlink_is_rejected_before_snapshotting(tmp_path):
    root = tmp_path / "repo"
    (root / "skills").mkdir(parents=True)
    (root / SKILL).symlink_to(ROOT / SKILL, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        InputVersions(root, None)


def test_existing_preset_instance_rediscovers_new_versioned_reference_files(tmp_path):
    root = tmp_path / "repo"
    for relative in PROMPT_SOURCES.values():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    shutil.copytree(ROOT / SKILL, root / SKILL, ignore=shutil.ignore_patterns("__pycache__"))
    versions = InputVersions(root, tmp_path / "run")
    first = versions.activate(versions.prepare(), "start")
    added = root / SKILL / "references/new-workflow.md"
    added.write_text("A newly deployed research workflow.")
    catalog_path = root / SKILL / "assets/catalog-entry.json"
    catalog = json.loads(catalog_path.read_text())
    catalog["version"] = "1.2.4"
    catalog_path.write_text(json.dumps(catalog))
    proposal = versions.prepare_update()
    name = f"{SKILL}/references/new-workflow.md"
    assert name not in first["files"]
    assert proposal["contents"][name] == added.read_text()
    assert proposal["files"][name]["version"] == "1.2.4"
    manifest = versions.activate(proposal, "hot_reload")
    assert (versions.run / "inputs" / name).read_text() == added.read_text()
    assert name in manifest["files"]
