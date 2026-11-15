"""Research files share one workspace while durable notes live outside it."""

import pytest

from a3_research_agent.host.published_harness import pin_run, research_workspace, run_metadata


def test_research_workspace_is_shared_across_run_identities(tmp_path):
    runs = [tmp_path / "trajectories" / name for name in ("first", "second")]
    for run in runs:
        run.mkdir(parents=True)
        pin_run(run, "a" * 40)
    assert research_workspace(runs[0], "main") == tmp_path / "workspace"
    assert research_workspace(runs[1], "another") == tmp_path / "workspace"
    assert "workspace_root" not in run_metadata(runs[0])


def test_memory_uses_the_host_root_across_working_directories(tmp_path, monkeypatch):
    from a3_research_agent.harness import long_term_memory as memory

    root = tmp_path / "memory"
    monkeypatch.setenv("A3_MEMORY_ROOT", str(root))
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    note = memory.write_memory("Use the saved checkpoint", refs=["job-original"])
    assert (root / "entries.jsonl").is_file()
    monkeypatch.chdir(second)
    assert memory.search_memory("checkpoint")[0]["id"] == note["id"]
    assert not (first / "memory.jsonl").exists()
    assert not (second / "memory.jsonl").exists()


def test_memory_requires_a_host_configured_root(tmp_path, monkeypatch):
    from a3_research_agent.harness import long_term_memory as memory

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("A3_MEMORY_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="Host must configure"):
        memory.write_memory("Preserve this finding")
    assert list(tmp_path.iterdir()) == []
