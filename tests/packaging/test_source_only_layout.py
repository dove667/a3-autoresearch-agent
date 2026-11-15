"""The maintained checkout and delivered Harness use one package layout."""

from pathlib import Path

from a3_research_agent.packaging.harness_payload import build_harness_payload

ROOT = Path(__file__).resolve().parents[2]


def test_payload_contains_only_current_package_sources():
    files = build_harness_payload()
    assert "a3_research_agent/harness/agent.py" in files
    assert all(name.startswith("a3_research_agent/") for name in files)
    assert not any("/orchestration/" in name for name in files)
