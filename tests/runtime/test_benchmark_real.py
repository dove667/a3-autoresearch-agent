"""Contracts for the real-Host benchmark without starting a Host."""

import json

import pytest

from a3_research_agent.benchmark import real
from a3_research_agent.harness.plan import store as plan_store
from a3_research_agent.host.sessions.processes import benchmark_root


def test_task_ids_and_plans_are_valid():
    ids = [task.id for task in real.TASKS]
    assert len(ids) == len(set(ids))
    for task in real.TASKS:
        plan = real._plan_for(task)
        encoded = json.dumps(plan, ensure_ascii=False).encode()
        assert plan_store.decode(encoded)["steps"][task.id]["route"] == task.route
        assert 1 <= len(task.task) <= 200
        assert 1 <= len(task.criterion) <= 240
        assert task.instructions.strip()


def test_materialize_is_a_thin_shared_root(tmp_path, monkeypatch):
    task = next(item for item in real.TASKS if item.id == "read_config")
    root = real.materialize(task, tmp_path / "task", model="test-model")
    assert (root / "workspace/experiments/2026-11-03-act-baseline/config.json").is_file()
    assert (root / "plan/current.json").is_file()
    assert not (root / "memory").exists()
    assert not (root / "skills").exists()
    assert (root / ".git").is_symlink()
    assert (root / "prompt").is_symlink()
    env = (root / ".env").read_text()
    assert "A3_BENCHMARK_MEMORY_ROOT=" in env
    assert "A3_BENCHMARK_SKILLS_ROOT=" in env
    assert "A3_UPSTREAM_RESPONSES_MODEL=test-model" in env


def test_read_config_score_requires_the_real_value(tmp_path):
    root = tmp_path
    target = root / "workspace/results/bench-config.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"seed_start":21000,"development_seeds":[31000,31001,31002]}')
    passed, _ = real._read_config_score(root, {}, root / "run")
    assert passed
    target.write_text('{"seed_start":0,"development_seeds":[]}')
    passed, _ = real._read_config_score(root, {}, root / "run")
    assert not passed


def test_benchmark_root_rejects_relative_or_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("A3_BENCHMARK_MEMORY_ROOT", "relative/path")
    with pytest.raises(ValueError):
        benchmark_root("A3_BENCHMARK_MEMORY_ROOT", tmp_path)
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir)
    monkeypatch.setenv("A3_BENCHMARK_MEMORY_ROOT", str(link))
    with pytest.raises(ValueError):
        benchmark_root("A3_BENCHMARK_MEMORY_ROOT", tmp_path)
    monkeypatch.setenv("A3_BENCHMARK_MEMORY_ROOT", str(real_dir))
    assert benchmark_root("A3_BENCHMARK_MEMORY_ROOT", tmp_path) == real_dir.resolve()


def test_benchmark_knowledge_guard_rejects_writes(tmp_path, monkeypatch):
    from a3_research_agent.harness.memory import dispatch as memory_dispatch
    from a3_research_agent.harness.skill_registry import dispatch as skill_dispatch

    monkeypatch.setenv("A3_BENCHMARK_READONLY_KNOWLEDGE", "1")
    assert memory_dispatch({"action": "write", "content": "x"}, root=tmp_path)["error"] == (
        "benchmark_read_only"
    )
    assert skill_dispatch({"action": "save", "id": "x"})["error"] == "benchmark_read_only"
