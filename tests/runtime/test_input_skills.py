"""Deployed reference skills stay discoverable without worker packaging."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from a3_research_agent.harness import input_skills, skill_registry

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_VERSIONS = {"a3-mujoco-autoresearch": "0.1.16", "a3-policy-integration": "0.1.0"}


def write_input_manifest(inputs, revision):
    files = {
        path.relative_to(inputs).as_posix(): {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()
        }
        for path in sorted((inputs / "skills").rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }
    (inputs / "input-manifest.json").write_text(
        json.dumps({"status": "active", "revision": revision, "files": files})
    )


@pytest.fixture(params=["a3-mujoco-autoresearch", "a3-policy-integration"])
def deployed_skill_upgrade(tmp_path, monkeypatch, request):
    """Use complete real packages with an earlier version and legacy instruction fixture."""
    identifier = request.param
    old_inputs, new_inputs = tmp_path / "old-inputs", tmp_path / "new-inputs"
    for inputs in (old_inputs, new_inputs):
        shutil.copytree(
            ROOT / "skills" / identifier,
            inputs / "skills" / identifier,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    catalog_path = old_inputs / "skills" / identifier / "assets/catalog-entry.json"
    catalog = json.loads(catalog_path.read_text())
    deployed_version = catalog["version"]
    catalog["version"] = PREVIOUS_VERSIONS[identifier]
    catalog_path.write_text(json.dumps(catalog))
    body = old_inputs / "skills" / identifier / "SKILL.md"
    body.write_text(
        body.read_text().replace(
            f"version: {deployed_version}", f"version: {catalog['version']}", 1
        )
        + "\nReview goal.md, acceptance.md and protocol.md before research.\n"
    )
    write_input_manifest(old_inputs, "before-maintenance")
    write_input_manifest(new_inputs, "after-maintenance")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    return identifier, old_inputs, new_inputs


def test_unedited_deployed_upgrade_keeps_the_canonical_id_and_archives_old_reference(
    deployed_skill_upgrade,
):
    identifier, old_inputs, new_inputs = deployed_skill_upgrade
    input_skills.install_input_skills(old_inputs)
    old_entry = skill_registry.dispatch({"action": "read", "id": identifier})
    old_tree = Path(old_entry["local_source"]).parent
    original = {path: path.read_bytes() for path in old_tree.rglob("*") if path.is_file()}
    input_skills.install_input_skills(new_inputs)
    current = skill_registry.dispatch({"action": "read", "id": identifier})
    assert (
        Path(current["local_source"]).read_bytes()
        == (new_inputs / "skills" / identifier / "SKILL.md").read_bytes()
    )
    assert current["local_source"] != old_entry["local_source"]
    entries = json.loads(Path("skills/index.json").read_text())["skills"]
    archived = [entry for entry in entries if entry["status"] == "archived"]
    assert len(archived) == 1
    assert archived[0]["id"] != identifier
    assert archived[0]["local_source"] == old_entry["local_source"]
    assert all(path.read_bytes() == content for path, content in original.items())
    published = Path("skills/index.json").read_bytes()
    input_skills.install_input_skills(new_inputs)
    assert Path("skills/index.json").read_bytes() == published


@pytest.mark.parametrize("revision", ["direct-edit", "skills-save", "unchanged-save"])
def test_deployed_upgrade_preserves_agent_revision_and_installs_a_separate_reference(
    deployed_skill_upgrade, revision
):
    identifier, old_inputs, new_inputs = deployed_skill_upgrade
    input_skills.install_input_skills(old_inputs)
    before = skill_registry.dispatch({"action": "read", "id": identifier})
    body = Path(before["local_source"])
    content = body.read_text() if revision == "unchanged-save" else "Agent-verified research method"
    if revision == "direct-edit":
        body.write_text(content)
    else:
        saved = skill_registry.dispatch(
            {
                "action": "save",
                "id": identifier,
                "description": "Agent-reviewed method",
                "content": content,
                "source": "retained research evidence",
                "expected_sha256": before["sha256"],
            }
        )
        assert "error" not in saved
    canonical_before = json.loads(Path("skills/index.json").read_text())["skills"][0]
    input_skills.install_input_skills(new_inputs)
    entries = json.loads(Path("skills/index.json").read_text())["skills"]
    assert next(entry for entry in entries if entry["id"] == identifier) == canonical_before
    assert body.read_text() == content
    deployed = next(entry for entry in entries if entry["id"] != identifier)
    assert deployed["status"] == "reference"
    assert (
        Path(deployed["local_source"]).read_bytes()
        == (new_inputs / "skills" / identifier / "SKILL.md").read_bytes()
    )
    published = Path("skills/index.json").read_bytes()
    input_skills.install_input_skills(new_inputs)
    assert Path("skills/index.json").read_bytes() == published


def test_archived_matching_tree_does_not_suppress_a_deployed_reference(deployed_skill_upgrade):
    identifier, old_inputs, _ = deployed_skill_upgrade
    input_skills.install_input_skills(old_inputs)
    current = skill_registry.dispatch({"action": "read", "id": identifier})
    archived = skill_registry.dispatch(
        {"action": "archive", "id": identifier, "expected_sha256": current["sha256"]}
    )
    assert archived["status"] == "archived"
    input_skills.install_input_skills(old_inputs)
    visible = skill_registry.dispatch({"action": "list"})["skills"]
    assert len(visible) == 1
    assert visible[0]["status"] == "reference"
    assert visible[0]["id"] != identifier
    published = Path("skills/index.json").read_bytes()
    input_skills.install_input_skills(old_inputs)
    assert Path("skills/index.json").read_bytes() == published


def test_existing_new_reference_repairs_an_unedited_stale_canonical_id(deployed_skill_upgrade):
    identifier, old_inputs, new_inputs = deployed_skill_upgrade
    index = Path("skills/index.json")
    input_skills.install_input_skills(old_inputs)
    original = json.loads(index.read_text())["skills"][0]
    input_skills.install_input_skills(new_inputs)
    latest = next(
        entry for entry in json.loads(index.read_text())["skills"] if entry["id"] == identifier
    )
    # Earlier installers left the old canonical entry beside an independent newer reference.
    independent_id = (
        f"{identifier}-reference-{latest['version']}-{latest['deployed_tree_sha256'][:8]}"
    )
    catalog = json.loads(index.read_text())
    catalog["skills"] = [original, {**latest, "id": independent_id}]
    index.write_text(json.dumps(catalog))
    preserved = {
        path: path.read_bytes()
        for entry in catalog["skills"]
        for path in Path(entry["local_source"]).parent.rglob("*")
        if path.is_file()
    }
    input_skills.install_input_skills(new_inputs)
    current = skill_registry.dispatch({"action": "read", "id": identifier})
    assert current["local_source"] == latest["local_source"]
    entries = json.loads(index.read_text())["skills"]
    assert len(entries) == 2
    assert next(entry for entry in entries if entry["id"] != identifier)["status"] == "archived"
    assert all(path.read_bytes() == content for path, content in preserved.items())
    published = index.read_bytes()
    input_skills.install_input_skills(new_inputs)
    assert index.read_bytes() == published


@pytest.mark.parametrize("change", ["edit", "add", "remove"])
def test_deployed_upgrade_preserves_agent_changes_to_reference_assets(
    deployed_skill_upgrade, change
):
    identifier, old_inputs, new_inputs = deployed_skill_upgrade
    index = Path("skills/index.json")
    input_skills.install_input_skills(old_inputs)
    original = json.loads(index.read_text())["skills"][0]
    body = Path(original["local_source"])
    assets = [path for path in body.parent.rglob("*") if path.is_file() and path != body]
    if change == "edit":
        assets[0].write_bytes(assets[0].read_bytes() + b"\nAgent-reviewed asset update\n")
    elif change == "add":
        (body.parent / "research-notes.md").write_text("Agent-verified method details")
    else:
        assets[0].unlink()
    preserved = {path: path.read_bytes() for path in body.parent.rglob("*") if path.is_file()}
    input_skills.install_input_skills(new_inputs)
    entries = json.loads(index.read_text())["skills"]
    assert next(entry for entry in entries if entry["id"] == identifier) == original
    assert len(entries) == 2
    assert all(path.read_bytes() == content for path, content in preserved.items())
    deployed = next(entry for entry in entries if entry["id"] != identifier)
    assert (
        Path(deployed["local_source"]).read_bytes()
        == (new_inputs / "skills" / identifier / "SKILL.md").read_bytes()
    )


def test_installed_reference_is_readable_idempotent_and_preserves_agent_revision(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    inputs = tmp_path / "inputs"
    root = inputs / "skills/research-method"
    (root / "assets").mkdir(parents=True)
    body = root / "SKILL.md"
    body.write_text("# Research method\nRead actual evidence.")
    entry = root / "assets/catalog-entry.json"
    entry.write_text(
        json.dumps(
            {
                "id": "research-method",
                "version": "0.1.0",
                "status": "reference",
                "local_source": "skills/research-method/SKILL.md",
                "description": "Evidence review",
                "evidence": [],
            }
        )
    )
    (inputs / "input-manifest.json").write_text(
        json.dumps(
            {
                "status": "active",
                "revision": "r1",
                "files": {
                    str(p.relative_to(inputs)): {
                        "sha256": hashlib.sha256(p.read_bytes()).hexdigest()
                    }
                    for p in [body, entry]
                },
            }
        )
    )
    monkeypatch.setenv("A3_INPUT_ROOT", str(inputs))
    input_skills.install_input_skills()
    first = (tmp_path / "skills/index.json").read_bytes()
    input_skills.install_input_skills(inputs)
    assert (tmp_path / "skills/index.json").read_bytes() == first
    current = skill_registry.dispatch({"action": "read", "id": "research-method"})
    assert current["content"] == body.read_text()
    result = skill_registry.dispatch(
        {
            "action": "save",
            "id": "research-method",
            "description": "Refined",
            "content": "Use refined guidance",
            "source": "experiment",
            "expected_sha256": current["sha256"],
        }
    )
    assert "error" not in result
    input_skills.install_input_skills(inputs)
    assert (
        skill_registry.dispatch({"action": "read", "id": "research-method"})["content"]
        == "Use refined guidance"
    )
    body.write_text("Unexpected changed source")
    # Already-installed unchanged snapshots stay idempotent; use a new revision to revalidate.
    manifest = json.loads((inputs / "input-manifest.json").read_text())
    manifest["files"]["skills/research-method/SKILL.md"]["sha256"] = "0" * 64
    (inputs / "input-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="changed"):
        input_skills.install_input_skills(inputs)
