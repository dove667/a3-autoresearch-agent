"""Deployed reference skills stay discoverable without worker packaging."""

import hashlib
import json

import pytest

from a3_research_agent.harness import input_skills, skill_registry


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
    input_skills.install_input_skills(inputs)
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
