"""Versioned research snapshots freeze inputs and publish operator updates atomically."""

import json

import pytest
from input_source_support import deployment_source

from a3_research_agent.host.inputs import versions as module
from a3_research_agent.host.inputs.versions import InputVersions


def setup_inputs(tmp_path):
    root = deployment_source(tmp_path / "source")
    run = tmp_path / "run"
    versions = InputVersions(root, run)
    versions.activate(versions.prepare(), "start")
    return root, run, versions


def test_resume_is_frozen_and_explicit_update_preserves_original_revision(tmp_path):
    root, run, versions = setup_inputs(tmp_path)
    first = versions.manifest
    archive = run / "input-revisions" / first["revision"] / "SOUL.md"
    original = archive.read_bytes()
    (root / "prompt/SOUL.md").write_text("Version: 1.0.1\nUpdated research principles\n")
    assert InputVersions(root, run).prepare()["digest"] == first["digest"]
    proposal = versions.prepare_update()
    assert proposal["digest"] != first["digest"]
    assert "Updated research principles" not in versions.model_instructions()[1]
    second = versions.activate(proposal, "hot_reload")
    assert second["revision"] != first["revision"]
    assert archive.read_bytes() == original
    assert "Updated research principles" in versions.model_instructions()[1]


def test_update_requires_incremented_prompt_versions(tmp_path):
    root, _, versions = setup_inputs(tmp_path)
    (root / "prompt/SOUL.md").write_text("Version: 1.0.0\nChanged without version\n")
    with pytest.raises(ValueError, match="version increment"):
        versions.prepare_update()
    (root / "prompt/SOUL.md").write_text("Missing version")
    with pytest.raises(ValueError, match="semantic version"):
        versions.prepare_update()


def test_snapshot_integrity_detects_changed_recorded_bytes(tmp_path):
    root, run, versions = setup_inputs(tmp_path)
    frozen = run / "input-revisions" / versions.manifest["revision"] / "SOUL.md"
    frozen.write_text("Version: 1.0.1\nChanged snapshot\n")
    with pytest.raises(ValueError, match="snapshot changed"):
        InputVersions(root, run).prepare()


def test_failed_mount_update_restores_inputs_manifest_and_events(tmp_path, monkeypatch):
    root, run, versions = setup_inputs(tmp_path)
    before = {path: path.read_bytes() for path in (run / "inputs").rglob("*") if path.is_file()}
    (root / "prompt/SOUL.md").write_text("Version: 1.0.1\nUpdated\n")
    proposal = versions.prepare_update()
    original = module.write_atomic
    failed = False

    def interrupted(path, content):
        nonlocal failed
        if path == run / "inputs/SOUL.md" and not failed:
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


def test_update_removes_retired_task_inputs_and_preserves_their_archive(tmp_path):
    root = deployment_source(tmp_path / "source")
    run = tmp_path / "run"
    historical = InputVersions(root, None).prepare()
    for name in ("goal.md", "acceptance.md", "protocol.md"):
        historical["contents"][name] = "Version: 1.0.0\nHistorical task material\n"
        historical["files"][name] = {"source": name}
    versions = InputVersions.from_proposal(root, run, historical)
    first = versions.activate(versions.prepare(), "historical_start")
    versions.activate(versions.prepare_update(), "maintenance_release")
    for name in ("goal.md", "acceptance.md", "protocol.md"):
        assert not (run / "inputs" / name).exists()
        assert (
            "Historical task material"
            in (run / "input-revisions" / first["revision"] / name).read_text()
        )
    assert "acceptance_checks" not in versions.manifest
