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
        json.loads((workspace / "checkpoint.json").read_text())[0]["content"]
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
