"""Check published research guidance and its frozen input mapping offline."""

import json
import re
import shutil
from pathlib import Path

import yaml

from a3_research_agent.host.inputs.versions import InputVersions, input_sources

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "skills/a3-mujoco-autoresearch"


def test_research_catalog_maps_to_readable_reference():
    entry = json.loads((PACKAGE / "assets/catalog-entry.json").read_text())
    body = (ROOT / entry["local_source"]).read_text()
    frontmatter = yaml.safe_load(body.split("---", 2)[1])
    assert entry["id"] == frontmatter["name"] == PACKAGE.name
    assert entry["version"] == frontmatter["metadata"]["version"]
    assert entry["status"] == "reference"
    assert entry["comparison_status"] == "compare-not-proven"
    assert len(body.splitlines()) <= 100
    for target in re.findall(r"\]\(([^)]+)\)", body):
        if "://" not in target:
            assert (PACKAGE / target.partition("#")[0]).is_file(), target
    assert "Use the deployed Harness" in body
    assert "/evolution/trials" not in body


def test_optional_research_skill_remains_frozen_and_is_not_injected(tmp_path):
    source = tmp_path / "custom-inputs"
    source.mkdir()
    for name, relative in input_sources("phase-two").items():
        (source / name).write_bytes((ROOT / relative).read_bytes())
    shutil.copytree(
        PACKAGE,
        source / "skills" / PACKAGE.name,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.tmp"),
    )
    versions = InputVersions(ROOT, tmp_path / "run", phase="phase-two", custom_dir=source)
    proposal = versions.prepare()
    versions.activate(proposal, "start")
    relative = "skills/a3-mujoco-autoresearch/SKILL.md"
    assert versions.contents[relative] == (ROOT / relative).read_text()
    assert versions.contents[relative] not in versions.model_instructions()[1]
    shutil.rmtree(source)
    restored = InputVersions(ROOT, versions.run)
    assert restored.prepare()["contents"][relative] == proposal["contents"][relative]
