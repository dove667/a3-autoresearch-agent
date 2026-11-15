"""Keep persistent research notes isolated from the real project during tests."""

import pytest


@pytest.fixture(autouse=True)
def isolated_long_term_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("A3_MEMORY_ROOT", str(tmp_path / "memory"))
