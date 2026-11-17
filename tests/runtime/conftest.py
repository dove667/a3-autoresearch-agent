"""Keep persistent research notes isolated from the real project during tests."""

import pytest


@pytest.fixture(autouse=True)
def isolated_long_term_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("A3_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path / "conversation"))
    monkeypatch.setenv("A3_PLAN_FILE", str(tmp_path / "plan/current.json"))
    monkeypatch.setenv("A3_PLAN_SNAPSHOTS_ROOT", str(tmp_path / "plan-snapshots"))
    monkeypatch.setenv("A3_SKILLS_ROOT", str(tmp_path / "skills"))
    monkeypatch.setenv("A3_SKILLS_HISTORY_ROOT", str(tmp_path / ".runtime/skill-revisions"))
    # A developer's local .env may describe the real deployment. Keep synthetic
    # release/resume tests hermetic: they set only the model settings they assert.
    for key in (
        "MODEL_NAME",
        "A3_MODEL_REASONING_EFFORT",
        "A3_WORKER_MODEL_NAME",
        "A3_WORKER_REASONING_EFFORT",
        "A3_SUMMARY_MODEL_NAME",
        "A3_SUMMARY_REASONING_EFFORT",
        "A3_MODEL_CONTEXT_TOKENS",
        "A3_WORKER_CONTEXT_TOKENS",
        "A3_SUMMARY_CONTEXT_TOKENS",
        "A3_CONTEXT_TRIGGER_TOKENS",
        "A3_CONTEXT_TARGET_TOKENS",
        "A3_OUTPUT_RESERVE_TOKENS",
        "A3_UPSTREAM_RESPONSES_BASE_URL",
        "A3_UPSTREAM_RESPONSES_MODEL",
        "A3_UPSTREAM_RESPONSES_API_KEY",
        "A3_UPSTREAM_RESPONSES_READ_TIMEOUT_SECONDS",
        "A3_UPSTREAM_DIRECT_INTERFACE",
        "A3_UPSTREAM_DIRECT_DNS",
    ):
        monkeypatch.delenv(key, raising=False)
