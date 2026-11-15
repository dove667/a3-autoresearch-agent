"""Generic retained tasks have explicit complete inputs and fresh task state."""

import hashlib
import shutil
from pathlib import Path

from a3_research_agent.host.inputs.versions import InputVersions

ROOT = Path(__file__).resolve().parents[2]


def bundle(tmp_path):
    directory = tmp_path / "custom"
    directory.mkdir()
    (directory / "protocol.md").write_text("Version: 1.0.0\nProtocol\n")
    (directory / "acceptance.md").write_text("Version: 1.0.0\n\n## search_reuse. Evidence\n")
    (directory / "goal.md").write_text("Version: 1.0.0\n\nResearch a useful skill\n")
    (directory / "notes").mkdir()
    (directory / "notes/references.md").write_text("Project references\n")
    return directory


def test_custom_bundle_snapshots_all_files_and_freezes_resume(tmp_path):
    directory = bundle(tmp_path)
    (directory / "initial-feedback.md").write_text("New approved task, not resume F")
    run = tmp_path / "run"
    versions = InputVersions(ROOT, run, custom_dir=directory)
    proposal = versions.prepare()
    manifest = versions.activate(proposal, "start")
    assert manifest["acceptance_checks"] == ["search_reuse"]
    assert versions.acceptance_checks() == ("search_reuse",)
    for name in proposal["contents"]:
        assert (run / "inputs" / name).read_text() == proposal["contents"][name]
        assert (
            manifest["files"][name]["sha256"]
            == hashlib.sha256(proposal["contents"][name].encode()).hexdigest()
        )
    assert "Research a useful skill" in versions.model_instructions()[1]
    assert "Project references" not in versions.model_instructions()[1]  # Mounted, not injected.
    shutil.rmtree(directory)
    restored = InputVersions(ROOT, run)
    assert restored.prepare()["contents"] == proposal["contents"]
    assert restored.prepare()["digest"] == proposal["digest"]
    second = restored.activate(restored.prepare(), "resume")
    assert second["files"] == manifest["files"]
