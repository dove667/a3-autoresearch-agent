"""Resume accepts only an explicit current native Host checkpoint."""

import json

import pytest
from native_run_support import native_run

from a3_research_agent.host.runs.resume import load_resume_state


def test_missing_host_checkpoint_is_not_rebuilt_from_history(tmp_path):
    run, _, _, store, _ = native_run(tmp_path)
    store.path.unlink()
    (run / "trajectory.json").write_text('{"model":"configured/model"}')
    with pytest.raises(ValueError, match="saved Host checkpoint"):
        load_resume_state(run, store, "configured/model")
    assert not store.path.exists()


def test_native_resume_preserves_same_scope_and_checkpoint_without_writes(tmp_path):
    run, workspace, state, store, _ = native_run(tmp_path)
    before = {
        path.relative_to(run): path.read_bytes()
        for path in run.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    assert load_resume_state(run, store, state["model"]) == state
    assert all((run / name).read_bytes() == raw for name, raw in before.items())
    assert (
        json.loads((workspace / ".agent/checkpoint.json").read_text())[0]["content"]
        == "existing research and observations"
    )


@pytest.mark.parametrize("change", ["model", "pin", "schema", "backend", "completed", "identity"])
def test_invalid_native_resume_is_rejected_without_mutation(tmp_path, change):
    run, _, state, store, _ = native_run(tmp_path)
    model = state["model"]
    if change == "model":
        model = "different/model"
    elif change == "pin":
        state["harness_commit"] = "b" * 40
    elif change == "schema":
        state["version"] = 0
    elif change == "backend":
        state["backend"] = "docker"
    elif change == "completed":
        state["status"] = "completed"
    elif change == "identity":
        state["gateway"]["main_owner"] = "unregistered"
    store.save(state)
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        load_resume_state(run, store, model)
    assert store.path.read_bytes() == before


def test_original_startup_remains_initial_provenance_after_published_upgrade(tmp_path):
    run, _, state, store, _ = native_run(tmp_path)
    startup = run / "startup.json"
    startup.write_text(json.dumps({"run_id": run.name, "harness_commit": "b" * 40}))
    before = startup.read_bytes()
    assert load_resume_state(run, store, state["model"]) == state
    assert startup.read_bytes() == before


def test_relocated_trajectory_retains_resource_identity_and_research_workspace(tmp_path):
    from a3_research_agent.host.published_harness import (
        project_for_run,
        research_workspace,
        run_identity,
    )
    from a3_research_agent.host.runs.state import RunStateStore

    run, workspace, state, _, _ = native_run(tmp_path)
    startup = (run / "startup.json").read_bytes()
    checkpoint = (workspace / ".agent/checkpoint.json").read_bytes()
    relocated = run.with_name("2026-11-10_21-27-31-654793")
    run.rename(relocated)
    store = RunStateStore(relocated)

    assert project_for_run(relocated) == tmp_path / "project"
    assert run_identity(relocated) == state["run_id"]
    assert research_workspace(relocated, state["gateway"]["main_owner"]) == workspace
    assert load_resume_state(relocated, store, state["model"]) == state
    assert (workspace / ".agent/checkpoint.json").read_bytes() == checkpoint
    assert (relocated / "startup.json").read_bytes() == startup
    assert state["gateway"]["jobs"] == {"main": ["owned-job"]}


def test_retired_artifact_run_location_is_rejected(tmp_path):
    from a3_research_agent.host.published_harness import project_for_run

    with pytest.raises(ValueError, match="under trajectories"):
        project_for_run(tmp_path / "artifacts/rsi/run-existing")


@pytest.mark.parametrize("base", ["../other", "/workspace/run", "workspace/../other", "other/run"])
def test_persistent_workspace_mapping_cannot_escape_project_workspace(tmp_path, base):
    from a3_research_agent.host.published_harness import MARKER, research_workspace

    run, _, _, _, _ = native_run(tmp_path)
    marker = run / MARKER
    metadata = json.loads(marker.read_text())
    metadata["workspace_root"] = base
    marker.write_text(json.dumps(metadata))
    with pytest.raises(ValueError):
        research_workspace(run, "main")
