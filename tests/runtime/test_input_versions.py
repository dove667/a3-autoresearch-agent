"""Versioned task snapshots freeze inputs and publish operator updates atomically."""

import json
from pathlib import Path

import pytest
from task_input_support import task_inputs

from a3_research_agent.host.inputs import versions as module
from a3_research_agent.host.inputs.versions import InputVersions

ROOT = Path(__file__).resolve().parents[2]


def setup_inputs(tmp_path):
    task = task_inputs(tmp_path / "task")
    run = tmp_path / "run"
    versions = InputVersions(ROOT, run, custom_dir=task)
    versions.activate(versions.prepare(), "start")
    return task, run, versions


def test_resume_is_frozen_and_explicit_update_preserves_original_revision(tmp_path):
    task, run, versions = setup_inputs(tmp_path)
    first = versions.manifest
    archive = run / "input-revisions" / first["revision"] / "goal.md"
    original = archive.read_bytes()
    (task / "goal.md").write_text("Version: 1.0.1\nNew selected question\n")
    assert InputVersions(ROOT, run).prepare()["digest"] == first["digest"]
    proposal = versions.prepare_directory(task)
    assert proposal["digest"] != first["digest"]
    assert "New selected question" not in versions.model_instructions()[1]
    second = versions.activate(proposal, "hot_reload")
    assert second["revision"] != first["revision"]
    assert archive.read_bytes() == original
    assert "New selected question" in versions.model_instructions()[1]


def test_update_requires_new_versions_and_versioned_task_contract(tmp_path):
    task, _, versions = setup_inputs(tmp_path)
    (task / "goal.md").write_text("Version: 1.0.0\nChanged without version\n")
    with pytest.raises(ValueError, match="version increment"):
        versions.prepare_directory(task)
    (task / "goal.md").write_text("Missing version")
    with pytest.raises(ValueError, match="semantic version"):
        versions.prepare_directory(task)


def test_snapshot_integrity_detects_changed_recorded_bytes(tmp_path):
    _, run, versions = setup_inputs(tmp_path)
    frozen = run / "input-revisions" / versions.manifest["revision"] / "goal.md"
    frozen.write_text("Version: 1.0.1\nChanged snapshot\n")
    with pytest.raises(ValueError, match="snapshot changed"):
        InputVersions(ROOT, run).prepare()


def test_failed_mount_update_restores_inputs_manifest_and_events(tmp_path, monkeypatch):
    task, run, versions = setup_inputs(tmp_path)
    before = {path: path.read_bytes() for path in (run / "inputs").rglob("*") if path.is_file()}
    (task / "goal.md").write_text("Version: 1.0.1\nUpdated\n")
    proposal = versions.prepare_directory(task)
    original = module.write_atomic
    failed = False

    def interrupted(path, content):
        nonlocal failed
        if path == run / "inputs/goal.md" and not failed:
            failed = True
            raise OSError("controlled disk error")
        return original(path, content)

    monkeypatch.setattr(module, "write_atomic", interrupted)
    with pytest.raises(OSError, match="controlled disk"):
        versions.activate(proposal, "hot_reload")
    for path, content in before.items():
        if path.name != "input-manifest.json":
            assert path.read_bytes() == content
    manifest = json.loads((run / "inputs/input-manifest.json").read_text())
    assert manifest["status"] == "active"
    assert manifest["digest"] == versions.manifest["digest"]
    assert "Updated" not in versions.model_instructions()[1]


def test_removed_task_attachment_leaves_active_mount_and_original_revision_is_retained(tmp_path):
    task = task_inputs(tmp_path / "task")
    (task / "notes.md").write_text("Original research note")
    versions = InputVersions(ROOT, tmp_path / "run", custom_dir=task)
    first = versions.activate(versions.prepare(), "start")
    (task / "notes.md").unlink()
    versions.activate(versions.prepare_directory(task), "hot_reload")
    assert not (versions.run / "inputs/notes.md").exists()
    assert (
        versions.run / "input-revisions" / first["revision"] / "notes.md"
    ).read_text() == "Original research note"
