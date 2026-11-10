"""The maintained checkout and delivered Harness use one package layout."""

from pathlib import Path

from a3_research_agent.packaging.harness_payload import build_harness_payload

ROOT = Path(__file__).resolve().parents[2]


def test_checkout_has_no_legacy_runtime_tree():
    assert not (ROOT / "runtime").exists()
    assert not (ROOT / "src/a3_research_agent/_compat.py").exists()
    assert not (ROOT / "src/a3_research_agent/execution/controller/remote_jobs.py").exists()


def test_payload_contains_only_package_sources_and_explicit_extensions():
    files = build_harness_payload({"skill_tools.py": "TOOLS = []\n"})
    assert "a3_research_agent/harness/agent.py" in files
    assert all(name.startswith("a3_research_agent/") or name == "skill_tools.py" for name in files)
    assert not any("/orchestration/" in name for name in files)
