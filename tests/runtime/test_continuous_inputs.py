"""Continuous research snapshots only current prompts."""

import hashlib
import shutil

from input_source_support import deployment_source

from a3_research_agent.host.inputs.versions import InputVersions


def test_current_inputs_freeze_resume_without_task_files(tmp_path):
    root = deployment_source(tmp_path / "source")
    (root / "goal.md").write_text("Historical objective")
    (root / "acceptance.md").write_text("Historical completion gate")
    (root / "protocol.md").write_text("Historical task profile")
    run = tmp_path / "run"
    versions = InputVersions(root, run)
    proposal = versions.prepare()
    manifest = versions.activate(proposal, "start")
    assert not {"goal.md", "acceptance.md", "protocol.md"} & proposal["contents"].keys()
    assert "acceptance_checks" not in manifest
    for name, content in proposal["contents"].items():
        assert (run / "inputs" / name).read_text() == content
        assert manifest["files"][name]["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    instructions = versions.model_instructions()[1]
    assert instructions.count(proposal["contents"]["SOUL.md"]) == 1
    assert "Historical objective" not in instructions
    shutil.rmtree(root)
    restored = InputVersions(root, run)
    assert restored.prepare()["contents"] == proposal["contents"]
    assert restored.prepare()["digest"] == proposal["digest"]
    second = restored.activate(restored.prepare(), "resume")
    assert second["files"] == manifest["files"]
