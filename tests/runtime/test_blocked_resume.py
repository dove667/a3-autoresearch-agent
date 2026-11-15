"""Explicit native blocked resumes preserve evidence and fence uncertain starts."""

import copy
import json

import pytest
from native_run_support import native_run

from a3_research_agent.host.runs import resume


def blocked(tmp_path):
    run, workspace, state, store, runtime = native_run(tmp_path, stopped=True)
    (workspace / "service-error.json").write_text('{"error":"service unavailable"}')
    outcome = {
        "status": "blocked",
        "external_blocker": {
            "kind": "unavailable_service",
            "requires_human": True,
            "reason": "model service unavailable",
            "required_action": "restore the model service",
            "evidence": ["service-error.json"],
        },
    }
    for path in (workspace / "run-outcome.json", run / "outcome.json"):
        path.write_text(json.dumps(outcome))
    (run / "supervision.json").write_text(json.dumps({"phase": "stopped", "outcome": outcome}))
    return run, workspace, state, store, runtime


def test_explicit_native_blocked_resume_preserves_checkpoint_and_scope(tmp_path):
    run, workspace, state, store, runtime = blocked(tmp_path)
    original = (workspace / "checkpoint.json").read_bytes()
    plan = resume.preflight_blocked_resume(run, state)
    attempt = resume.begin_blocked_resume(run, state, store, runtime, plan, {"revision": "current"})
    assert (workspace / "checkpoint.json").read_bytes() == original
    token = state["gateway"]["session_records"]["main"]["token"]
    assert token != "main-token" and state["gateway"]["sessions"][token] == "main"
    assert state["gateway"]["sessions"]["root-token"] == run.name
    assert "main-token" not in state["gateway"]["sessions"]
    assert runtime.records["main"]["self_stopped"] is False
    assert json.loads((attempt / "run-outcome.json").read_text())["status"] == "blocked"
    assert json.loads((workspace / "run-outcome.json").read_text())["status"] == "continue"
    with pytest.raises(ValueError, match="pending"):
        resume.preflight_blocked_resume(run, store.load())
    resume.finish_blocked_resume(attempt)
    assert json.loads((attempt / "attempt.json").read_text())["status"] == "complete"


@pytest.mark.parametrize(
    "change",
    [
        "host_running",
        "closed",
        "running",
        "uncertain_launch",
        "evidence",
        "checkpoint",
        "pending_call",
        "duplicate_call",
        "orphan_result",
        "pin",
    ],
)
def test_native_blocked_preflight_rejects_invalid_state_without_writes(tmp_path, change):
    run, workspace, state, _store, runtime = blocked(tmp_path)
    record = runtime.records["main"]
    if change == "host_running":
        state["status"] = "running"
    elif change == "closed":
        record["closed"] = True
    elif change == "running":
        record["terminal_state"]["status"] = "running"
    elif change == "uncertain_launch":
        record["launch_pending"] = True
    elif change == "evidence":
        (workspace / "service-error.json").unlink()
    elif change == "checkpoint":
        (workspace / "checkpoint.json").write_text("{}")
    elif change == "pending_call":
        (workspace / "checkpoint.json").write_text('[{"type":"function_call","call_id":"x"}]')
    elif change == "duplicate_call":
        (workspace / "checkpoint.json").write_text(
            '[{"type":"function_call","call_id":"x"},{"type":"function_call","call_id":"x"},{"type":"function_call_output","call_id":"x"}]'
        )
    elif change == "orphan_result":
        (workspace / "checkpoint.json").write_text(
            '[{"type":"function_call_output","call_id":"x"}]'
        )
    elif change == "pin":
        state["harness_commit"] = "b" * 40
    runtime.save_record(record)
    before = copy.deepcopy(state)
    with pytest.raises(ValueError):
        resume.preflight_blocked_resume(run, state)
    assert state == before and not (run / ".host/blocked-resume").exists()


def test_partial_authorization_write_failure_keeps_pending_fence_and_credentials(
    tmp_path, monkeypatch
):
    run, workspace, state, store, runtime = blocked(tmp_path)
    before = copy.deepcopy(state)
    plan = resume.preflight_blocked_resume(run, state)
    write = resume.write_durable

    def fail(path, content):
        if path == workspace / "run-outcome.json":
            raise OSError("controlled disk failure")
        return write(path, content)

    monkeypatch.setattr(resume, "write_durable", fail)
    with pytest.raises(OSError, match="disk failure"):
        resume.begin_blocked_resume(run, state, store, runtime, plan, {})
    assert state == before and store.load() == before
    with pytest.raises(ValueError, match="pending"):
        resume.preflight_blocked_resume(run, state)
