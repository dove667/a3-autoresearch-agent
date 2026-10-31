import json

import pytest

from runtime.local.run_resume import load_resume_state
from runtime.local.run_state import RunStateStore


def old_run(tmp_path):
    run = tmp_path / "run-original-timestamp"
    (run / "inputs").mkdir(parents=True)
    (run / "startup.json").write_text(json.dumps({"run_id": run.name, "model": "test/model"}))
    (run / "inputs/deployment.json").write_text(json.dumps({"gateway": "http://original:12345"}))
    (run / "evolution.json").write_text(
        json.dumps({"main_owner": "candidate-main", "trials": {}, "transitions": []})
    )
    directory = run / "candidates/candidate-main"
    directory.mkdir(parents=True)
    (directory / "session.json").write_text(
        json.dumps({"owner": "candidate-main", "closed": True, "version_id": "old-version"})
    )
    (run / "session-events.jsonl").write_text(
        json.dumps({"event": "created", "session_id": "candidate-main", "parent": run.name}) + "\n"
    )
    return run


def test_import_old_host_metadata_preserves_scope_directory_and_endpoint(tmp_path):
    run = old_run(tmp_path)
    before = {p.relative_to(run): p.read_bytes() for p in run.rglob("*") if p.is_file()}
    state = load_resume_state(run, RunStateStore(run), "test/model")
    assert state["run_id"] == run.name
    assert state["endpoint"] == "http://original:12345"
    assert state["gateway"]["evolution"]["main_owner"] == "candidate-main"
    assert "candidate-main" in state["gateway"]["sessions"].values()
    assert state["gateway"]["children"]["candidate-main"]["version_id"] == "old-version"
    assert all((run / name).read_bytes() == data for name, data in before.items())
    assert list(tmp_path.iterdir()) == [run]
    assert load_resume_state(run, RunStateStore(run), "test/model") == state


def test_resume_refuses_changed_model_without_creating_a_new_run(tmp_path):
    run = old_run(tmp_path)
    with pytest.raises(ValueError, match="original model"):
        load_resume_state(run, RunStateStore(run), "other/model")
    assert list(tmp_path.iterdir()) == [run]
    assert not RunStateStore(run).path.exists()


def test_saved_host_rejects_adapter_change(tmp_path):
    run = old_run(tmp_path)
    store = RunStateStore(run)
    load_resume_state(run, store, "test/model", "original_adapter")
    with pytest.raises(ValueError, match="adapter"):
        load_resume_state(run, store, "test/model", "changed_adapter")
