"""One controlled project plan spans runs; each run retains immutable revisions."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from a3_research_agent.harness import agent, paths
from a3_research_agent.harness import plan as planning
from a3_research_agent.harness.plan import store as plan_store


def first_plan():
    return planning.dispatch(
        {
            "action": "update",
            "revision": 0,
            "direction": {"objective": "Improve full evaluation"},
            "steps": [
                {"id": "evaluate", "task": "Evaluate candidate", "criterion": "Saved results"}
            ],
        }
    )


def test_plan_continues_across_runs_without_loading_their_old_snapshots(tmp_path, monkeypatch):
    first = tmp_path / "trajectories/first/plan-snapshots"
    second = tmp_path / "trajectories/second/plan-snapshots"
    monkeypatch.setenv("A3_PLAN_SNAPSHOTS_ROOT", str(first))
    assert first_plan()["revision"] == 1
    before = {p.name: p.read_bytes() for p in first.glob("*.json")}
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path / "trajectories/second/conversation"))
    monkeypatch.setenv("A3_PLAN_SNAPSHOTS_ROOT", str(second))
    old = tmp_path / "trajectories/second/conversation/research-plan.json"
    old.parent.mkdir(parents=True)
    old.write_text('{"version":1,"revision":0,"goal":"obsolete","reason":"","steps":{}}')
    assert (
        planning.dispatch({"action": "show"})["direction"]["objective"] == "Improve full evaluation"
    )
    result = planning.dispatch(
        {"action": "update", "revision": 1, "steps": [{"id": "evaluate", "status": "active"}]}
    )
    assert result["revision"] == 2
    assert json.loads(paths.plan_file().read_text())["steps"]["evaluate"]["status"] == "active"
    assert {p.name: p.read_bytes() for p in first.glob("*.json")} == before
    assert {json.loads(p.read_text())["revision"] for p in second.glob("*.json")} == {1, 2}
    monkeypatch.setenv("A3_PLAN_SNAPSHOTS_ROOT", str(first))
    assert planning.dispatch({"action": "show"})["revision"] == 2
    assert json.loads(old.read_text())["goal"] == "obsolete"


def test_parallel_updates_recheck_revision_under_the_shared_file_lock():
    assert first_plan()["revision"] == 1
    arguments = {
        "action": "update",
        "revision": 1,
        "steps": [{"id": "evaluate", "status": "active"}],
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(planning.dispatch, [arguments, arguments]))
    assert sum(result.get("error") == "stale_plan_revision" for result in results) == 1
    assert planning.load()["revision"] == 2


def test_missing_plan_configuration_never_falls_back_to_the_trajectory(tmp_path, monkeypatch):
    monkeypatch.delenv("A3_PLAN_FILE")
    old = tmp_path / "conversation/research-plan.json"
    old.parent.mkdir()
    old.write_text('{"version":1,"revision":99}')
    with pytest.raises(RuntimeError, match="Host must configure"):
        planning.load()


def test_plan_uses_controlled_tools_outside_the_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("A3_WORKSPACE", str(workspace))
    monkeypatch.setenv("A3_INPUT_ROOT", str(tmp_path / "inputs"))
    monkeypatch.setenv("A3_REFERENCE_ROOT", str(tmp_path / "reference"))
    assert first_plan()["revision"] == 1
    before = paths.plan_file().read_bytes()
    assert (
        agent.tool("write", {"path": str(paths.plan_file()), "content": "overwritten"})["error"]
        == "path_not_accessible"
    )
    edited = agent.tool(
        "edit", {"path": str(paths.plan_file()), "old_text": "Improve", "new_text": "Discard"}
    )
    assert edited["error"] == "path_not_accessible"
    assert paths.plan_file().read_bytes() == before


def test_snapshots_preserve_original_bytes_and_reject_changed_archives(tmp_path):
    source = tmp_path / "plan.json"
    raw = (
        '{"version":2, "revision":198,"direction":{"objective":"继续研究"}, "steps":{}}\n'.encode()
    )
    source.write_bytes(raw)
    directory = tmp_path / "trajectories/run/plan-snapshots"
    snapshot = plan_store.snapshot(source, directory)
    assert snapshot.read_bytes() == raw
    assert source.read_bytes() == raw
    assert plan_store.snapshot(source, directory) == snapshot
    snapshot.write_text("modified archive")
    with pytest.raises(ValueError, match="snapshot content changed"):
        plan_store.snapshot(source, directory)
    assert source.read_bytes() == raw
    assert snapshot.read_text() == "modified archive"


def test_missing_plan_is_empty_without_creating_source_or_snapshots(tmp_path):
    source = tmp_path / "plan.json"
    directory = tmp_path / "trajectories/run/plan-snapshots"
    assert plan_store.load(source)["revision"] == 0
    assert plan_store.snapshot(source, directory) is None
    assert not source.exists()
    assert not directory.exists()


@pytest.mark.parametrize("variable", ["A3_PLAN_FILE", "A3_PLAN_SNAPSHOTS_ROOT"])
def test_plan_configuration_requires_absolute_non_symlink_paths(tmp_path, monkeypatch, variable):
    resolve = paths.plan_file if variable == "A3_PLAN_FILE" else paths.plan_snapshots_root
    monkeypatch.setenv(variable, "relative/path")
    with pytest.raises(RuntimeError, match="Host must configure"):
        resolve()
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv(variable, str(link / "plan.json"))
    with pytest.raises(ValueError, match="symbolic links"):
        resolve()
