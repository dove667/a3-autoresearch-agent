"""Keep persistent research notes isolated from the real project during tests."""

import pytest


@pytest.fixture(autouse=True)
def isolated_long_term_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("A3_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path / "conversation"))
    monkeypatch.setenv("A3_PLAN_FILE", str(tmp_path / "plan.json"))
    monkeypatch.setenv("A3_PLAN_SNAPSHOTS_ROOT", str(tmp_path / "plan-snapshots"))
    monkeypatch.setenv("A3_SKILLS_ROOT", str(tmp_path / "skills"))
    monkeypatch.setenv("A3_SKILLS_HISTORY_ROOT", str(tmp_path / ".runtime/skill-revisions"))
