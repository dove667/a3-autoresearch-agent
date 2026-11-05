"""SOUL is a versioned main-role input, not a replacement for stage authority."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.inputs.versions import InputVersions, input_sources

PROFILES = [
    ("phase-one", "standard"),
    ("phase-one", "web-extension"),
    ("phase-one", "skills-extension"),
    ("phase-two", "standard"),
]


def source_bundle(tmp_path, phase="phase-one", profile="standard"):
    root = tmp_path / "source"
    for name, relative in input_sources(phase, profile).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"Version: 0.1.0\n{name} UNIQUE CONTENT\n"
        if name == "bootstrap.yaml":
            content = (
                'version: 0.1.0\nagent:\n  system_template: "{{ phase_instructions }}"\n'
                '  instance_template: "{{ task }}"\nrun:\n  task: task\n'
            )
        path.write_text(content)
    return root


@pytest.mark.parametrize("phase,profile", PROFILES)
def test_soul_is_published_hashed_and_forwarded_with_selected_stage(tmp_path, phase, profile):
    root = source_bundle(tmp_path, phase, profile)
    run = tmp_path / "run"
    (run / "private-inputs").mkdir(parents=True)
    inputs = InputVersions(root, run, phase=phase, task_profile=profile)
    manifest = inputs.activate(inputs.prepare(), "start")
    soul = (root / "agent-inputs/SOUL.md").read_bytes()
    assert manifest["files"]["SOUL.md"] == {
        "source": "agent-inputs/SOUL.md",
        "version": "0.1.0",
        "sha256": hashlib.sha256(soul).hexdigest(),
    }
    for directory in ("inputs", "private-inputs", f"input-revisions/{manifest['revision']}"):
        assert (run / directory / "SOUL.md").read_bytes() == soul
    calls = []
    client = TestClient(
        create_app(
            model_name="test/model",
            sessions={"token": "main"},
            main_owner="main",
            forward_model=lambda **kwargs: calls.append(kwargs) or {"output": []},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
            input_versions=inputs,
        )
    )
    response = client.post(
        "/v1/responses", headers={"Authorization": "Bearer token"}, json={"input": "Continue"}
    )
    assert response.status_code == 200
    instructions = calls[-1]["instructions"]
    assert soul.decode() in instructions
    assert "SOUL 不得覆盖当前阶段的 goal、acceptance、protocol 或权限边界" in instructions
    for name in ("goal.md", "acceptance.md") + (
        ("phase-instructions.md",) if phase == "phase-one" else ()
    ):
        assert inputs.contents[name] in instructions
    if phase == "phase-two":
        assert inputs.contents["protocol.md"] in instructions
    assert "execution.md UNIQUE CONTENT" not in instructions
    assert not any(item["source"].startswith("docs/") for item in manifest["files"].values())
    usage = json.loads((tmp_path / "usage.jsonl").read_text())
    assert usage["input_versions"]["SOUL.md"] == manifest["files"]["SOUL.md"]


def test_pre_soul_resume_adds_new_revision_without_rewriting_history(tmp_path):
    root = source_bundle(tmp_path)
    run = tmp_path / "run"
    old = InputVersions(root, run)
    old.sources.pop("SOUL.md", None)  # Reproduce the actual pre-SOUL input format.
    previous = old.activate(old.prepare(), "start")
    archive = run / "input-revisions" / previous["revision"]
    original = {p.name: p.read_bytes() for p in archive.iterdir()}
    checkpoint = run / "trajectory.json"
    checkpoint.write_text('{"history": "original context"}')
    resumed = InputVersions(root, run)
    current = resumed.activate(resumed.prepare(), "resume")
    assert "SOUL.md" in current["files"]
    assert "SOUL.md" not in previous["files"]
    assert {p.name: p.read_bytes() for p in archive.iterdir()} == original
    legacy = run / "input-revisions/legacy"
    assert json.loads((legacy / "input-manifest.json").read_text()) == previous
    assert not (legacy / "SOUL.md").exists()
    assert checkpoint.read_text() == '{"history": "original context"}'
    (root / "agent-inputs/SOUL.md").write_text("Version: 0.1.1\nupdated soul\n")
    updated = resumed.activate(resumed.prepare(), "hot_reload")
    assert updated["files"]["SOUL.md"]["version"] == "0.1.1"
    assert "updated soul" in resumed.model_instructions()[1]
    assert (
        run / "input-revisions" / current["revision"] / "SOUL.md"
    ).read_text() != "Version: 0.1.1\nupdated soul\n"
    assert {p.name: p.read_bytes() for p in archive.iterdir()} == original
    (root / "agent-inputs/SOUL.md").write_text("Version: 0.1.1\nchanged without bump\n")
    with pytest.raises(ValueError, match="SOUL.md changed without a version increment"):
        resumed.prepare()


def test_repository_phase_two_layers_remain_selected_and_versioned(tmp_path):
    """Exercise real input files, not synthetic layer placeholders."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    inputs = InputVersions(root, tmp_path / "run", phase="phase-two")
    manifest = inputs.activate(inputs.prepare(), "start")
    _, instructions = inputs.model_instructions()
    assert manifest["files"]["SOUL.md"]["version"] == "0.2.6"
    assert "phase-instructions.md" not in manifest["files"]
    for name in ("SOUL.md", "goal.md", "acceptance.md", "protocol.md"):
        assert inputs.contents[name] in instructions
    assert inputs.contents["execution.md"] not in instructions
    assert "包括验收在内的任务" in instructions
    assert "需要主身份的派发、验收协调和交权操作" in instructions
    assert "委派具体测试不等于委派验收责任" in instructions
    assert "不为简单协调机械再开 worker" in instructions
    assert not (root / "agent-inputs/phase-two/instructions.md").exists()
    # Optional method references and maintainer review notes are not injected inputs.
    assert not any(
        item["source"].startswith(("skills/", "docs/")) for item in manifest["files"].values()
    )
