import copy
import json

import pytest

from runtime.local import run_resume
from runtime.local.candidate_runtime import CandidateRuntime
from runtime.local.run_state import RunStateStore


def fixture(tmp_path):
    run = tmp_path / "run-original"
    workspace = run / "candidates/main/workspace"
    workspace.mkdir(parents=True)
    terminal = {
        "Status": "exited",
        "Running": False,
        "Paused": False,
        "Restarting": False,
        "ExitCode": 137,
    }
    record = {
        "owner": "main",
        "session_id": "main",
        "container_id": "old-container",
        "closed": False,
        "self_stop_requested": True,
        "self_stopped": True,
        "container_removed": True,
        "terminal_state": terminal,
        "version_id": "version",
    }
    (workspace.parent / "session.json").write_text(json.dumps(record))
    (workspace.parent / "console.log").write_text("old console")
    outcome = {
        "status": "blocked",
        "acceptance": {"E": {"status": "blocked"}},
        "external_blocker": {
            "kind": "unavailable_service",
            "requires_human": True,
            "reason": "service unavailable",
            "required_action": "restore service",
            "evidence": ["web-report.json"],
        },
    }
    (workspace / "run-outcome.json").write_text(json.dumps(outcome))
    (run / "outcome.json").write_text(json.dumps(outcome))
    (run / "supervision.json").write_text(json.dumps({"phase": "stopped", "outcome": outcome}))
    (workspace / "web-report.json").write_text('{"error":"unavailable"}')
    (workspace / "agent.py").write_text("pass")
    (workspace / "checkpoint.json").write_text(
        json.dumps(
            [
                {"role": "user", "content": "original"},
                {"type": "function_call", "call_id": "call-1", "name": "tool"},
                {"type": "function_call_output", "call_id": "call-1", "output": "original result"},
            ]
        )
    )
    (workspace / "resume.json").write_text(
        json.dumps(
            {
                "command": ["python", "agent.py", "--resume"],
                "files": ["agent.py", "checkpoint.json"],
            }
        )
    )
    state = {
        "run_id": run.name,
        "model": "model",
        "status": "suspended",
        "gateway": {
            "sessions": {"root-token": run.name, "other-token": "other"},
            "root_owners": [run.name],
            "children": {
                "main": {
                    "owner": "main",
                    "parent": run.name,
                    "token": "revoked-token",
                    "closed": False,
                    "self_stopped": True,
                }
            },
            "scope_parents": {"main": run.name},
            "jobs": {},
            "evolution": {"main_owner": "main", "trials": {}, "transitions": []},
        },
    }
    (run / "evolution.json").write_text(json.dumps(state["gateway"]["evolution"]))
    store = RunStateStore(run)
    store.save(state)
    runtime = CandidateRuntime(
        root=run / "candidates",
        image="image",
        inputs=run / "inputs",
        public_a3=run / "public",
        endpoint="http://original",
    )
    runtime.load_records()
    return run, workspace, state, store, runtime


def test_explicit_blocked_resume_preserves_checkpoint_and_rotates_only_main(tmp_path):
    run, workspace, state, store, runtime = fixture(tmp_path)
    original = (workspace / "checkpoint.json").read_bytes()
    plan = run_resume.preflight_blocked_resume(run, state)
    assert plan is not None
    attempt = run_resume.begin_blocked_resume(run, state, store, runtime, plan, {"revision": "r2"})
    assert (workspace / "checkpoint.json").read_bytes() == original
    assert json.loads((workspace / "run-outcome.json").read_text())["status"] == "continue"
    assert state["gateway"]["sessions"]["root-token"] == run.name
    assert state["gateway"]["sessions"]["other-token"] == "other"
    assert "revoked-token" not in state["gateway"]["sessions"]
    token = state["gateway"]["children"]["main"]["token"]
    assert token != "revoked-token"
    assert state["gateway"]["sessions"][token] == "main"
    assert runtime.records["main"]["self_stopped"] is False
    assert (attempt / "session.json").read_text() == (plan["record_text"])
    assert (attempt / "console.log").read_text() == "old console"
    assert json.loads((attempt / "run-outcome.json").read_text())["status"] == "blocked"
    with pytest.raises(ValueError, match="pending"):
        run_resume.preflight_blocked_resume(run, store.load())


@pytest.mark.parametrize(
    "change",
    [
        "completed",
        "closed",
        "requested",
        "running",
        "evidence",
        "checkpoint",
        "pending_call",
        "duplicate_call",
        "retired",
    ],
)
def test_blocked_preflight_rejects_without_mutating(tmp_path, change):
    run, workspace, state, _store, runtime = fixture(tmp_path)
    record = runtime.records["main"]
    if change == "completed":
        state["status"] = "completed"
    elif change == "closed":
        record["closed"] = True
    elif change == "requested":
        record["self_stopped"] = False
    elif change == "running":
        record["terminal_state"]["Running"] = True
    elif change == "evidence":
        (workspace / "web-report.json").unlink()
    elif change == "checkpoint":
        (workspace / "checkpoint.json").write_text("7")
    elif change == "pending_call":
        (workspace / "checkpoint.json").write_text('[{"type":"function_call","call_id":"x"}]')
    elif change == "duplicate_call":
        (workspace / "checkpoint.json").write_text(
            json.dumps(
                [
                    {"type": "function_call", "call_id": "x"},
                    {"type": "function_call", "call_id": "x"},
                    {"type": "function_call_output", "call_id": "x"},
                ]
            )
        )
    elif change == "retired":
        state["gateway"]["evolution"]["main_owner"] = "other"
    (workspace.parent / "session.json").write_text(json.dumps(record))
    before = copy.deepcopy(state)
    if change == "retired":
        assert run_resume.preflight_blocked_resume(run, state) is None
    else:
        with pytest.raises(ValueError):
            run_resume.preflight_blocked_resume(run, state)
    assert state == before
    assert not (run / ".host/blocked-resume").exists()


def fake_docker(monkeypatch, runtime, *, fail_create=False):
    calls = []
    status = {
        "Status": "running",
        "Running": True,
        "Paused": False,
        "Restarting": False,
        "ExitCode": 0,
    }

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "create":
            if fail_create:
                raise RuntimeError("controlled create failure")
            return "new-container"
        if args[0] == "inspect":
            return json.dumps(status)
        if args[0] == "logs":
            return "new console"
        if args[0] == "stop":
            status.update(Status="exited", Running=False)
        return ""

    monkeypatch.setattr(runtime, "docker", docker)
    return calls


def test_rotated_gateway_token_keeps_scope_and_can_self_stop_again(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from runtime.local.host_gateway import create_app

    run, workspace, state, store, runtime = fixture(tmp_path)
    calls = fake_docker(monkeypatch, runtime)
    original = (workspace / "checkpoint.json").read_bytes()
    plan = run_resume.preflight_blocked_resume(run, state)
    attempt = run_resume.begin_blocked_resume(run, state, store, runtime, plan, {"revision": "r2"})
    app = create_app(
        model_name="model",
        sessions={"root-token": run.name},
        main_owner=run.name,
        sandbox=runtime,
        state_store=store,
        usage_log=run / "usage.jsonl",
        restored_state=state["gateway"],
        forward_model=lambda **kwargs: {"output": []},
        remote_call=lambda *args: {},
    )
    runtime.resume_sessions(state["gateway"]["sessions"], "main")
    run_resume.finish_blocked_resume(attempt)
    assert sum(call[0] == "create" for call in calls) == 1
    create = next(call for call in calls if call[0] == "create")
    assert create[-3:] == ("python", "agent.py", "--resume")
    assert f"type=bind,src={workspace},dst=/workspace" in create
    assert runtime.records["main"]["version_id"] == "version"
    assert (workspace / "checkpoint.json").read_bytes() == original
    client = TestClient(app)
    old = {"Authorization": "Bearer revoked-token"}
    token = state["gateway"]["children"]["main"]["token"]
    new = {"Authorization": "Bearer " + token}
    assert client.get("/sessions/self", headers=old).status_code == 401
    assert client.get("/sessions/self", headers=new).json()["session_id"] == "main"
    assert client.get("/sessions/" + run.name, headers=new).status_code == 403
    assert (
        client.get("/sessions", headers={"Authorization": "Bearer other-token"}).status_code == 200
    )
    assert (
        client.post("/sessions/self/stop", headers=new, json={"reason": "saved again"}).status_code
        == 200
    )
    assert client.get("/sessions/self", headers=new).status_code == 401
    assert runtime.records["main"]["self_stopped"] is True
    assert json.loads((attempt / "session.json").read_text())["terminal_state"]["ExitCode"] == 137
    assert (attempt / "console.log").read_text() == "old console"
    assert json.loads((attempt / "attempt.json").read_text())["status"] == "complete"


def test_create_failure_keeps_pending_fence_and_history(tmp_path, monkeypatch):
    run, _workspace, state, store, runtime = fixture(tmp_path)
    calls = fake_docker(monkeypatch, runtime, fail_create=True)
    plan = run_resume.preflight_blocked_resume(run, state)
    attempt = run_resume.begin_blocked_resume(run, state, store, runtime, plan, {})
    with pytest.raises(RuntimeError, match="controlled create failure"):
        runtime.resume_sessions(state["gateway"]["sessions"], "main")
    assert not any(call[0] == "start" for call in calls)
    assert (attempt / "run-outcome.json").read_text() == plan["snapshots"]["run-outcome.json"]
    with pytest.raises(ValueError, match="pending"):
        run_resume.preflight_blocked_resume(run, store.load())
    assert sum(call[0] == "create" for call in calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        "evidence_path",
        "resume_path",
        "bad_command",
        "result_only",
        "duplicate_result",
        "empty_checkpoint",
        "host_running",
    ],
)
def test_unsafe_or_incomplete_preflight_is_read_only(tmp_path, change):
    run, workspace, state, store, runtime = fixture(tmp_path)
    if change == "evidence_path":
        outcome = json.loads((workspace / "run-outcome.json").read_text())
        outcome["external_blocker"]["evidence"] = ["../session.json"]
        (workspace / "run-outcome.json").write_text(json.dumps(outcome))
    elif change == "resume_path":
        manifest = json.loads((workspace / "resume.json").read_text())
        manifest["files"].append("../session.json")
        (workspace / "resume.json").write_text(json.dumps(manifest))
    elif change == "bad_command":
        manifest = json.loads((workspace / "resume.json").read_text())
        manifest["command"] = ["sh", "-c", "python agent.py"]
        (workspace / "resume.json").write_text(json.dumps(manifest))
    elif change == "result_only":
        (workspace / "checkpoint.json").write_text(
            '[{"type":"function_call_output","call_id":"x"}]'
        )
    elif change == "duplicate_result":
        messages = json.loads((workspace / "checkpoint.json").read_text())
        messages.append(messages[-1])
        (workspace / "checkpoint.json").write_text(json.dumps(messages))
    elif change == "empty_checkpoint":
        (workspace / "checkpoint.json").write_text("[]")
    elif change == "host_running":
        state["status"] = "running"
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        run_resume.preflight_blocked_resume(run, state)
    assert store.path.read_bytes() == before
    assert runtime.records["main"]["self_stopped"] is True


def test_stale_host_main_does_not_revive_retired_main(tmp_path):
    run, _workspace, state, _store, runtime = fixture(tmp_path)
    evolution = state["gateway"]["evolution"].copy()
    evolution["main_owner"] = "successor"
    (run / "evolution.json").write_text(json.dumps(evolution))
    with pytest.raises(ValueError, match="main identity"):
        run_resume.preflight_blocked_resume(run, state)
    assert runtime.records["main"]["self_stopped"] is True


def test_restart_attempt_appends_audit_without_credentials(tmp_path):
    import hashlib

    run, _workspace, state, store, runtime = fixture(tmp_path)
    events = run / "session-events.jsonl"
    old = '{"event":"self_stopped","session_id":"main"}\n'
    events.write_text(old)
    plan = run_resume.preflight_blocked_resume(run, state)
    run_resume.begin_blocked_resume(run, state, store, runtime, plan, {"revision": "r2"})
    assert events.read_text().startswith(old)
    event = json.loads(events.read_text().splitlines()[-1])
    assert event["event"] == "explicit_blocked_resume"
    assert event["main_owner"] == "main"
    assert event["input_manifest"]["revision"] == "r2"
    assert (
        event["previous_sha256"]["run-outcome.json"]
        == hashlib.sha256(plan["snapshots"]["run-outcome.json"].encode()).hexdigest()
    )
    assert "token" not in events.read_text()


def test_partial_transition_failure_is_fenced_before_credential_rotation(tmp_path, monkeypatch):
    run, workspace, state, store, runtime = fixture(tmp_path)
    before = copy.deepcopy(state)
    plan = run_resume.preflight_blocked_resume(run, state)
    original_write = CandidateRuntime.write_durable

    def write(path, content):
        if path == workspace / "run-outcome.json":
            raise OSError("controlled disk failure")
        original_write(path, content)

    monkeypatch.setattr(CandidateRuntime, "write_durable", staticmethod(write))
    with pytest.raises(OSError, match="controlled disk"):
        run_resume.begin_blocked_resume(run, state, store, runtime, plan, {})
    assert state == before
    assert store.load() == before
    assert runtime.records["main"]["self_stopped"] is True
    with pytest.raises(ValueError, match="pending"):
        run_resume.preflight_blocked_resume(run, state)


def test_launch_invalid_checkpoint_stops_before_inputs_gateway_or_workers(tmp_path, monkeypatch):
    from argparse import Namespace

    from runtime.local import launch

    run, workspace, state, store, _runtime = fixture(tmp_path)
    state["model_class"] = "adapter"
    store.save(state)
    (run / "startup.json").write_text("{}")
    (workspace / "checkpoint.json").write_text("7")

    class Inputs:
        def __init__(self, *args, **kwargs):
            pass

        def prepare(self):
            return {}

        def activate(self, *args):
            pytest.fail("must not activate inputs on invalid checkpoint")

    monkeypatch.setattr(launch, "InputVersions", Inputs)
    monkeypatch.setattr(
        launch, "create_app", lambda **kwargs: pytest.fail("no gateway before preflight")
    )
    monkeypatch.setattr(
        launch, "CandidateRuntime", lambda **kwargs: pytest.fail("no worker before preflight")
    )
    monkeypatch.setenv("MODEL_NAME", "model")
    monkeypatch.setenv("MODEL_CLASS", "adapter")
    with pytest.raises(ValueError, match="checkpoint"):
        launch.launch_run(
            Namespace(resume_from=run, verify_deployment=False), run, tmp_path, "image", None
        )
    assert store.load() == state


@pytest.mark.parametrize("change", ["missing_closed", "supervision_complete", "changed_resume"])
def test_conflicting_or_changed_recovery_metadata_is_rejected(tmp_path, change):
    run, workspace, state, store, runtime = fixture(tmp_path)
    plan = run_resume.preflight_blocked_resume(run, state)
    if change == "missing_closed":
        state["gateway"]["children"]["main"].pop("closed")
    elif change == "supervision_complete":
        (run / "supervision.json").write_text(
            json.dumps({"phase": "stopped", "outcome": {"status": "complete"}})
        )
    else:
        (workspace / "agent.py").write_text("pass # changed after preflight")
    with pytest.raises(ValueError):
        if change == "changed_resume":
            run_resume.begin_blocked_resume(run, state, store, runtime, plan, {})
        else:
            run_resume.preflight_blocked_resume(run, state)
    assert runtime.records["main"]["self_stopped"] is True
    assert not (run / ".host/blocked-resume").exists()
