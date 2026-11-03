"""Publication and input-snapshot mapping, without a model or live F runtime."""

import json
import re
import shutil
from pathlib import Path

import yaml

from runtime.local.input_versions import InputVersions, input_sources

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("research-orchestrator", "rsi-harness-evolution", "a3-mujoco-autoresearch")


def test_orchestrator_catalog_maps_to_versioned_readable_reference():
    package = ROOT / "skills/research-orchestrator"
    entry = json.loads((package / "assets/catalog-entry.json").read_text())
    body = (ROOT / entry["local_source"]).read_text()
    frontmatter = yaml.safe_load(body.split("---", 2)[1])
    assert entry["id"] == frontmatter["name"] == package.name
    assert entry["version"] == frontmatter["metadata"]["version"] == "0.1.1"
    assert entry["status"] == "reference"
    assert entry["comparison_status"] == "compare-not-proven"
    assert len(body.splitlines()) <= 100
    for target in re.findall(r"\]\(([^)]+)\)", body):
        assert (package / target).is_file(), target


def test_optional_orchestrator_and_dependencies_remain_frozen_input_files(tmp_path):
    source = tmp_path / "custom-inputs"
    source.mkdir()
    for name, relative in input_sources("phase-two").items():
        (source / name).write_bytes((ROOT / relative).read_bytes())
    for name in PACKAGES:
        shutil.copytree(
            ROOT / "skills" / name,
            source / "skills" / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.tmp"),
        )
    versions = InputVersions(ROOT, tmp_path / "run", phase="phase-two", custom_dir=source)
    proposal = versions.prepare()
    versions.activate(proposal, "start")
    relative = "skills/research-orchestrator/SKILL.md"
    assert versions.contents[relative] == (ROOT / relative).read_text()
    assert versions.contents[relative] not in versions.model_instructions()[1]
    shutil.rmtree(source)
    restored = InputVersions(ROOT, versions.run)
    assert restored.prepare()["contents"][relative] == proposal["contents"][relative]
