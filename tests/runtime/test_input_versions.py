import json

import pytest

from a3_research_agent.host.inputs.versions import InputVersions, input_sources

INPUT_SOURCES = input_sources("phase-one")


def sources(tmp_path):
    root = tmp_path / "source"
    for name, relative in INPUT_SOURCES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"version: 0.1.0\n\n{name} current content\n")
        if name == "bootstrap.yaml":
            path.write_text(
                'version: 0.1.0\nagent:\n  system_template: "{{ phase_instructions }}"\n  instance_template: "{{ task }}"\nrun:\n  task: task\n'
            )
    return root


def test_start_and_resume_load_current_files_without_overwriting_original_archive(tmp_path):
    root = sources(tmp_path)
    run = tmp_path / "run"
    legacy = run / "inputs"
    legacy.mkdir(parents=True)
    (legacy / "goal.md").write_text("old unversioned goal")
    versions = InputVersions(root, run)
    initial = versions.activate(versions.prepare(), "resume")
    assert initial["files"]["goal.md"]["version"] == "0.1.0"
    assert "current content" in (legacy / "goal.md").read_text()
    assert (run / "input-revisions/legacy/goal.md").read_text() == "old unversioned goal"
    previous = run / "input-revisions" / initial["revision"] / "goal.md"
    original = previous.read_bytes()
    (root / INPUT_SOURCES["goal.md"]).write_text("version: 0.1.1\nnew goal\n")
    second = InputVersions(root, run).activate(InputVersions(root, run).prepare(), "resume")
    assert second["files"]["goal.md"]["version"] == "0.1.1"
    assert previous.read_bytes() == original
    assert "new goal" in (legacy / "goal.md").read_text()


def test_modified_content_needs_a_new_version_and_missing_versions_fail(tmp_path):
    root = sources(tmp_path)
    versions = InputVersions(root, tmp_path / "run")
    versions.activate(versions.prepare(), "start")
    goal = root / INPUT_SOURCES["goal.md"]
    goal.write_text("version: 0.1.0\nchanged content\n")
    with pytest.raises(ValueError, match="version"):
        versions.prepare()
    goal.write_text("no version header")
    with pytest.raises(ValueError, match="version"):
        versions.prepare()


def test_private_mounts_receive_same_revision_and_context_files_are_untouched(tmp_path):
    root = sources(tmp_path)
    run = tmp_path / "run"
    private = run / "private-inputs"
    private.mkdir(parents=True)
    checkpoint = run / "checkpoint.json"
    checkpoint.write_text('[{"role":"user","content":"keep original context"}]')
    versions = InputVersions(root, run)
    manifest = versions.activate(versions.prepare(), "resume")
    assert (private / "goal.md").read_bytes() == (run / "inputs/goal.md").read_bytes()
    assert (
        json.loads((private / "input-manifest.json").read_text())["revision"]
        == manifest["revision"]
    )
    assert checkpoint.read_text() == '[{"role":"user","content":"keep original context"}]'
    metadata, instructions = versions.model_instructions()
    assert metadata["revision"] == manifest["revision"]
    assert "goal.md current content" in instructions
    assert "acceptance.md current content" in instructions


def test_prepare_does_not_activate_or_replace_live_prompt(tmp_path):
    root = sources(tmp_path)
    versions = InputVersions(root, tmp_path / "run")
    initial = versions.activate(versions.prepare(), "start")
    prompt = root / INPUT_SOURCES["phase-instructions.md"]
    prompt.write_text("version: 0.1.1\nupdated prompt\n")
    preview = versions.prepare()
    assert preview["digest"] != initial["digest"]
    assert versions.manifest["revision"] == initial["revision"]
    assert "updated prompt" not in versions.model_instructions()[1]


def test_failed_private_mount_update_restores_all_active_files(tmp_path, monkeypatch):
    from a3_research_agent.host.inputs import versions as input_versions

    root = sources(tmp_path)
    run = tmp_path / "run"
    (run / "private-inputs").mkdir(parents=True)
    versions = InputVersions(root, run)
    first = versions.activate(versions.prepare(), "start")
    before = {p: p.read_bytes() for p in (run / "inputs").iterdir()}
    (root / INPUT_SOURCES["goal.md"]).write_text("Version: 0.1.1\nnew goal\n")
    real_write = input_versions.write_atomic
    fail = True

    def write(path, content):
        nonlocal fail
        if path == run / "private-inputs/goal.md" and fail:
            fail = False
            raise OSError("controlled write failure")
        real_write(path, content)

    monkeypatch.setattr(input_versions, "write_atomic", write)
    with pytest.raises(OSError, match="controlled"):
        versions.activate(versions.prepare(), "hot_reload")
    assert versions.manifest["revision"] == first["revision"]
    assert versions.manifest["generation"] > first["generation"]
    assert versions.manifest["files"] == first["files"]
    for path, data in before.items():
        if path.name != "input-manifest.json":
            assert path.read_bytes() == data


@pytest.mark.parametrize(
    "bootstrap",
    [
        "version: 0.1.1\nagent: [",
        "version: 0.1.1\nagent: {}\nrun: {}\n",
        'version: 0.1.1\nagent:\n  system_template: "{{ broken"\n  instance_template: task\nrun:\n  task: task\n',
    ],
)
def test_invalid_bootstrap_is_rejected_before_activation(tmp_path, bootstrap):
    root = sources(tmp_path)
    versions = InputVersions(root, tmp_path / "run")
    first = versions.activate(versions.prepare(), "start")
    (root / INPUT_SOURCES["bootstrap.yaml"]).write_text(bootstrap)
    with pytest.raises(ValueError, match="bootstrap"):
        versions.prepare()
    assert versions.manifest == first


def test_multifile_update_is_marked_before_any_new_contents_are_visible(tmp_path, monkeypatch):
    from a3_research_agent.host.inputs import versions as input_versions

    root = sources(tmp_path)
    run = tmp_path / "run"
    (run / "private-inputs").mkdir(parents=True)
    versions = InputVersions(root, run)
    versions.activate(versions.prepare(), "start")
    (root / INPUT_SOURCES["goal.md"]).write_text("Version: 0.1.1\nnew goal\n")
    real_write = input_versions.write_atomic
    observed = []

    def write(path, content):
        real_write(path, content)
        if path == run / "inputs/goal.md":
            observed.extend(
                json.loads((run / directory / "input-manifest.json").read_text())["status"]
                for directory in ("inputs", "private-inputs")
            )

    monkeypatch.setattr(input_versions, "write_atomic", write)
    versions.activate(versions.prepare(), "hot_reload")
    assert observed == ["updating", "updating"]
