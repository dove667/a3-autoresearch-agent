"""SOUL is versioned, frozen and forwarded once."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from input_source_support import deployment_source

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.inputs.versions import InputVersions


def test_soul_is_snapshotted_hashed_and_forwarded_with_current_research(tmp_path):
    root = deployment_source(tmp_path / "source")
    run = tmp_path / "run"
    inputs = InputVersions(root, run)
    manifest = inputs.activate(inputs.prepare(), "start")
    soul = (root / "prompt/SOUL.md").read_bytes()
    assert manifest["files"]["SOUL.md"] == {
        "source": "prompt/SOUL.md",
        "version": "1.0.0",
        "sha256": hashlib.sha256(soul).hexdigest(),
    }
    for directory in ("inputs", f"input-revisions/{manifest['revision']}"):
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
    assert instructions.count(inputs.contents["SOUL.md"]) == 1
    assert "EXECUTION REFERENCE UNIQUE" not in instructions
    assert "SKILL REFERENCE UNIQUE" not in instructions
    usage = json.loads((tmp_path / "usage.jsonl").read_text())
    assert usage["input_versions"]["SOUL.md"] == manifest["files"]["SOUL.md"]


def test_shared_update_is_explicit_and_preserves_original_revision(tmp_path):
    root = deployment_source(tmp_path / "source")
    run = tmp_path / "run"
    inputs = InputVersions(root, run)
    original = inputs.activate(inputs.prepare(), "start")
    archived = run / "input-revisions" / original["revision"] / "SOUL.md"
    before = archived.read_bytes()
    source = root / "prompt/SOUL.md"
    source.write_text("Version: 1.0.1\nUpdated SOUL principles\n")
    restored = InputVersions(root, run)
    assert restored.prepare()["digest"] == original["digest"]
    proposal = restored.prepare_update()
    assert proposal["digest"] != original["digest"]
    current = restored.activate(proposal, "hot_reload")
    assert current["files"]["SOUL.md"]["version"] == "1.0.1"
    assert "Updated SOUL principles" in restored.model_instructions()[1]
    assert archived.read_bytes() == before
    source.write_text("Version: 1.0.1\nChanged without version increment\n")
    with pytest.raises(ValueError, match="version increment"):
        restored.prepare_update()
    assert archived.read_bytes() == before
