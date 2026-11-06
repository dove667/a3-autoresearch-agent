"""Source relocation preserves mounted input names, contents and run history."""

import hashlib
import json
from pathlib import Path

import pytest

from a3_research_agent.host.inputs.versions import InputVersions, input_sources

ROOT = Path(__file__).resolve().parents[2]
# Frozen source paths and content hashes from 5abaf41, before relocation.
LEGACY = json.loads((Path(__file__).parent / "fixtures/pre_unified_input_sources.json").read_text())
RELOCATED_EXECUTION = (Path(__file__).parent / "fixtures/relocated_execution_0_8_2.md").read_bytes()
PROFILES = [("phase-one", "standard"), ("phase-one", "web-extension"), ("phase-two", "standard")]


EXECUTION_RELOCATIONS = {
    "Version: 0.8.1": "Version: 0.8.2",
    "configs/rsi-bootstrap.yaml": "agent-inputs/shared/bootstrap.yaml",
    "configs/prompts/phase-one.md": "agent-inputs/phase-one/instructions.md",
    "configs/prompts/phase-two.md": "agent-inputs/phase-two/instructions.md",
    "evals/<phase>/": "agent-inputs/<phase>/",
    "configs/prompts/phase-one-web.md": "agent-inputs/phase-one-web/instructions.md",
    "evals/phase-one-web/{goal,acceptance}.md": "agent-inputs/phase-one-web/{goal,acceptance}.md",
}


def relocation_fixture_content(name, path):
    # This regression targets the historical relocation, not later input revisions.
    # Keep the frozen migration's content pinned against the pre-relocation hashes.
    if name == "execution.md":
        return RELOCATED_EXECUTION
    frozen = {
        "agent-inputs/phase-two/acceptance.md": "relocated_phase_two_acceptance_0_3_0.md",
        "agent-inputs/phase-two/goal.md": "relocated_phase_two_goal_0_2_0.md",
        "agent-inputs/phase-two/protocol.md": "relocated_phase_two_protocol_0_3_0.md",
        "agent-inputs/shared/bootstrap.yaml": "relocated_bootstrap_0_1_1.yaml",
        "agent-inputs/SOUL.md": "relocated_soul_0_1_0.md",
        "agent-inputs/phase-two/instructions.md": "relocated_phase_two_0_7_0.md",
        # Verbatim source blobs from e9800c60c3011239825bcbbfaf01f1fbda50e209.
        # These were unchanged by relocation; the fixed pre-relocation SHA256
        # below verifies their provenance independently of current templates.
        "agent-inputs/shared/readiness-task.md": "relocated_readiness_task_0_1_1.md",
        "agent-inputs/shared/workspace.md": "relocated_workspace_0_1_0.md",
        "agent-inputs/phase-one/goal.md": "relocated_phase_one_goal_0_2_0.md",
        "agent-inputs/phase-one/acceptance.md": "relocated_phase_one_acceptance_0_3_0.md",
        "agent-inputs/phase-one/instructions.md": "relocated_phase_one_instructions_0_3_0.md",
        "agent-inputs/phase-one-web/goal.md": "relocated_phase_one_web_goal_0_2_0.md",
        "agent-inputs/phase-one-web/acceptance.md": "relocated_phase_one_web_acceptance_0_2_0.md",
        "agent-inputs/phase-one-web/instructions.md": "relocated_phase_one_web_instructions_0_2_0.md",
    }
    if path in frozen:
        return (Path(__file__).parent / "fixtures" / frozen[path]).read_bytes()
    raise AssertionError(f"historical relocation source has no pinned fixture: {path}")


def previous_content(name, content):
    if name == "execution.md":
        for old, new in EXECUTION_RELOCATIONS.items():
            content = content.replace(new.encode(), old.encode())
    return content


def expected_sources(bundle, historical=False):
    sources = {
        "SOUL.md": "agent-inputs/SOUL.md",
        "bootstrap.yaml": "agent-inputs/shared/bootstrap.yaml",
        "execution.md": "agent-inputs/shared/execution.md",
        "readiness-task.md": "agent-inputs/shared/readiness-task.md",
        "workspace-instructions.md": "agent-inputs/shared/workspace.md",
        "goal.md": f"agent-inputs/{bundle}/goal.md",
        "acceptance.md": f"agent-inputs/{bundle}/acceptance.md",
        "phase-instructions.md": f"agent-inputs/{bundle}/instructions.md",
    }
    if bundle == "phase-two" and not historical:
        sources.pop("phase-instructions.md")
    if bundle == "phase-two":
        sources["protocol.md"] = (
            "agent-inputs/phase-two/protocol.md"
            if historical
            else "agent-inputs/shared/protocol.md"
        )
    return sources


@pytest.mark.parametrize("phase,profile", PROFILES)
def test_active_source_paths_and_historical_relocation_contents(phase, profile):
    bundle = "phase-one-web" if profile == "web-extension" else phase
    assert input_sources(phase, profile) == expected_sources(bundle)
    for name, path in expected_sources(bundle, historical=True).items():
        content = previous_content(name, relocation_fixture_content(name, path))
        assert hashlib.sha256(content).hexdigest() == LEGACY[bundle][name]["sha256"]


@pytest.mark.parametrize("phase,profile", PROFILES)
def test_relocated_sources_resume_old_manifest_without_rewriting_history(tmp_path, phase, profile):
    bundle = "phase-one-web" if profile == "web-extension" else phase
    root, run = tmp_path / "sources", tmp_path / "run"
    for name, path in expected_sources(bundle, historical=True).items():
        content = relocation_fixture_content(name, path)
        for destination in (path, LEGACY[bundle][name]["source"]):
            target = root / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(previous_content(name, content) if destination != path else content)
    (run / "private-inputs").mkdir(parents=True)
    (run / "workspace/harness").mkdir(parents=True)
    old = InputVersions(root, run, phase=phase, task_profile=profile)
    old.sources = {name: item["source"] for name, item in LEGACY[bundle].items()}
    previous = old.activate(old.prepare(), "start")
    old_contents = old.contents.copy()
    old_body = old.model_instructions()[1].split("\n", 1)[1]
    archive = run / "input-revisions" / previous["revision"]
    original = {p.name: p.read_bytes() for p in archive.iterdir()}
    for item in LEGACY[bundle].values():
        (root / item["source"]).unlink()
    checkpoint = run / "trajectory.json"
    checkpoint.write_text('{"history": "original context"}')
    resumed = InputVersions(root, run)
    # Replay the original relocation schema, not the later removal of Phase 2's
    # instructions layer; its removal has a separate migration regression.
    resumed.sources = expected_sources(bundle, historical=True)
    current = resumed.activate(resumed.prepare(), "resume")
    assert current["phase"] == phase
    assert current["task_profile"] == profile
    assert current["revision"] != previous["revision"]
    assert current["digest"] != previous["digest"]
    assert {
        name: previous_content(name, text.encode()).decode()
        for name, text in resumed.contents.items()
    } == old_contents
    assert resumed.model_instructions()[1].split("\n", 1)[1] == old_body
    assert {p.name: p.read_bytes() for p in archive.iterdir()} == original
    assert json.loads((run / "input-revisions/legacy/input-manifest.json").read_text()) == previous
    assert checkpoint.read_text() == '{"history": "original context"}'
    for name, item in current["files"].items():
        assert item["source"] == expected_sources(bundle, historical=True)[name]
        if name == "execution.md":
            assert previous["files"][name]["version"] == "0.8.1"
            assert item["version"] == "0.8.2"
            assert item["sha256"] != previous["files"][name]["sha256"]
        else:
            assert item["version"] == previous["files"][name]["version"]
            assert item["sha256"] == previous["files"][name]["sha256"]
        for target in ("inputs", "private-inputs", f"input-revisions/{current['revision']}"):
            assert (run / target / name).read_text() == resumed.contents[name]
    assert (run / "workspace/harness/AGENTS.md").read_text() == old_contents[
        "workspace-instructions.md"
    ]


def test_bootstrap_workspace_reads_unified_guide(tmp_path, monkeypatch):
    from a3_research_agent.host import bootstrap as launch

    # Keep checkout creation local; exercise the real workspace guide loader.
    monkeypatch.setattr(
        launch, "snapshot_checkout", lambda source, target: target.mkdir(parents=True)
    )
    workspace = tmp_path / "workspace"
    launch.prepare_workspace(workspace, tmp_path / "a3")
    assert (workspace / "harness/AGENTS.md").read_bytes() == (
        ROOT / "agent-inputs/shared/workspace.md"
    ).read_bytes()
