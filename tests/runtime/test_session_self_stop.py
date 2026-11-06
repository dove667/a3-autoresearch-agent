import json

import pytest
from fastapi.testclient import TestClient
from host_fixtures import host_create_session

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.runs.state import RunStateStore
from a3_research_agent.host.sessions.containers import CandidateRuntime


class SessionBackend:
    def __init__(self):
        self.tokens = {}
        self.records = {}
        self.fail_stop = False

    def create(self, owner, command, files, token):
        self.tokens[owner] = token
        self.records[owner] = {"status": "running"}
        return {"session_id": owner, "status": "created"}

    def status(self, owner, session):
        assert owner == session
        return {"session_id": session, **self.records[session]}

    def stop(self, owner, session):
        assert owner == session
        if self.fail_stop:
            raise RuntimeError("controlled Docker failure")
        self.records[session]["status"] = "exited"
        return {"session_id": session, "status": "exited"}


def setup(tmp_path):
    backend = SessionBackend()
    remote_calls = []
    store = RunStateStore(tmp_path)
    app = create_app(
        model_name="test/model",
        sessions={"root-token": "root"},
        main_owner="root",
        sandbox=backend,
        state_store=store,
        usage_log=tmp_path / "usage.jsonl",
        forward_model=lambda **kwargs: {"output": []},
        remote_call=lambda op, payload: remote_calls.append((op, payload)) or {"job_id": "job"},
    )
    client = TestClient(app)
    root = {"Authorization": "Bearer root-token"}
    session = host_create_session(client, headers=root, json={"command": ["true"]}).json()[
        "session_id"
    ]
    own = {"Authorization": "Bearer " + backend.tokens[session]}
    return client, backend, store, remote_calls, root, session, own


def test_self_status_is_readable_without_granting_ancestor_access(tmp_path):
    client, _, _, _, root, session, own = setup(tmp_path)
    response = client.get("/sessions/self", headers=own)
    assert response.status_code == 200
    assert response.json()["session_id"] == session
    assert client.get("/sessions/self", headers=root).status_code == 403
    assert client.get("/sessions/root", headers=own).status_code == 403


def test_self_stop_preserves_descendants_jobs_and_parent_access(tmp_path):
    client, backend, _, remote_calls, root, session, own = setup(tmp_path)
    child = host_create_session(client, headers=own, json={"command": ["true"]}).json()[
        "session_id"
    ]
    child_auth = {"Authorization": "Bearer " + backend.tokens[child]}
    client.post("/jobs", headers=own, json={"command": ["true"]})
    response = client.post("/sessions/self/stop", headers=own, json={"reason": "work saved"})
    assert response.status_code == 200, response.text
    assert response.json() == {"session_id": session, "status": "exited"}
    assert backend.records[child]["status"] == "running"
    assert client.get("/sessions/self", headers=child_auth).status_code == 200
    assert client.get(f"/sessions/{session}", headers=root).json()["status"] == "exited"
    assert client.get(f"/sessions/{child}", headers=root).json()["status"] == "running"
    assert client.post("/v1/responses", headers=own, json={"input": "again"}).status_code == 401
    assert [op for op, _ in remote_calls] == ["submit"]
    saved = json.loads((tmp_path / ".host/state.json").read_text())["gateway"]
    assert saved["children"][session]["self_stopped"] is True
    assert saved["jobs"][session] == ["job"]
    events = [
        json.loads(line) for line in (tmp_path / "session-events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["event"] == "self_stopped"


def test_self_stop_failure_keeps_identity_available_for_retry(tmp_path):
    client, backend, _, _, _, session, own = setup(tmp_path)
    backend.fail_stop = True
    assert (
        client.post("/sessions/self/stop", headers=own, json={"reason": "saved"}).status_code == 409
    )
    assert client.get("/sessions/self", headers=own).json()["status"] == "running"
    backend.fail_stop = False
    assert (
        client.post("/sessions/self/stop", headers=own, json={"reason": "saved"}).status_code == 200
    )
    assert backend.records[session]["status"] == "exited"


def test_self_stop_cannot_target_or_cascade_into_other_sessions(tmp_path):
    client, _, _, _, root, _, own = setup(tmp_path)
    for extra in ({"session_id": "root"}, {"cascade": True}):
        assert (
            client.post(
                "/sessions/self/stop", headers=own, json={"reason": "saved", **extra}
            ).status_code
            == 422
        )
    assert client.post("/sessions/self/stop", json={"reason": "saved"}).status_code == 401
    assert (
        client.post("/sessions/self/stop", headers=root, json={"reason": "saved"}).status_code
        == 403
    )


def test_runtime_self_stop_preserves_workspace_and_is_not_resumed(tmp_path, monkeypatch):
    runtime = CandidateRuntime(
        root=tmp_path / "candidates",
        image="image",
        inputs=tmp_path,
        public_a3=tmp_path,
        endpoint="http://gateway",
    )
    directory = runtime.root / "main"
    workspace = directory / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "memory.txt").write_text("preserve")
    record = {"owner": "main", "session_id": "main", "container_id": "container", "closed": False}
    runtime.records["main"] = record
    commands = []
    monkeypatch.setattr(
        runtime, "docker", lambda *args, **kwargs: commands.append(args) or "console"
    )
    monkeypatch.setattr(runtime, "state", lambda record: {"Status": "exited", "ExitCode": 137})
    result = runtime.stop("main", "main")
    assert result["status"] == "exited"
    assert any(command[0] == "stop" for command in commands)
    assert any(command[0] == "rm" for command in commands)
    assert (workspace / "memory.txt").read_text() == "preserve"
    assert (directory / "console.log").read_text() == "console"
    assert json.loads((directory / "session.json").read_text())["self_stopped"] is True
    monkeypatch.setattr(runtime, "state", lambda record: {"Status": "exited"})
    commands.clear()
    runtime.resume_sessions({}, "main")
    assert commands == []
    with pytest.raises(PermissionError):
        runtime.stop("other", "main")


def test_log_failure_after_stop_does_not_turn_termination_into_restart(tmp_path, monkeypatch):
    import subprocess

    runtime = CandidateRuntime(
        root=tmp_path,
        image="image",
        inputs=tmp_path,
        public_a3=tmp_path,
        endpoint="http://gateway",
    )
    (tmp_path / "main/workspace").mkdir(parents=True)
    runtime.records["main"] = {"owner": "main", "container_id": "container", "closed": False}

    def docker(*args, **kwargs):
        if args[0] == "logs":
            raise subprocess.TimeoutExpired("docker logs", 30)
        return ""

    monkeypatch.setattr(runtime, "docker", docker)
    monkeypatch.setattr(runtime, "state", lambda record: {"Status": "exited", "ExitCode": 137})
    assert runtime.stop("main", "main")["status"] == "exited"
    saved = json.loads((tmp_path / "main/session.json").read_text())
    assert saved["self_stopped"] is True
    assert saved["cleanup_error"] == "TimeoutExpired"
    assert not saved.get("container_removed")
    runtime.resume_sessions({}, "main")


def test_unconfirmed_stop_intent_refuses_automatic_recovery(tmp_path, monkeypatch):
    runtime = CandidateRuntime(
        root=tmp_path,
        image="image",
        inputs=tmp_path,
        public_a3=tmp_path,
        endpoint="http://gateway",
    )
    runtime.records["main"] = {
        "owner": "main",
        "container_id": "container",
        "closed": False,
        "self_stop_requested": True,
    }
    monkeypatch.setattr(runtime, "state", lambda record: {"Status": "exited"})
    with pytest.raises(RuntimeError, match="self-stop"):
        runtime.resume_sessions({}, "main")


def test_gateway_recovery_reconciles_stop_completed_before_gateway_saved(tmp_path):
    _, backend, store, _, root, session, own = setup(tmp_path)
    saved = store.load()["gateway"]
    # Docker/runtime saved the stop, but the Host died before revoking the token.
    backend.records[session] = {"status": "exited", "self_stopped": True}
    recovered = TestClient(
        create_app(
            model_name="test/model",
            sessions={},
            main_owner="root",
            sandbox=backend,
            usage_log=tmp_path / "usage.jsonl",
            state_store=store,
            restored_state=saved,
            forward_model=lambda **kwargs: {"output": []},
            remote_call=lambda *args: {},
        )
    )
    assert recovered.post("/v1/responses", headers=own, json={"input": "again"}).status_code == 401
    assert recovered.get(f"/sessions/{session}", headers=root).json()["status"] == "exited"


def test_cancelled_worker_stop_request_does_not_block_main_recovery(tmp_path):
    runtime = CandidateRuntime(
        root=tmp_path,
        image="image",
        inputs=tmp_path,
        public_a3=tmp_path,
        endpoint="http://gateway",
    )
    runtime.records["cancelled-worker"] = {
        "owner": "cancelled-worker",
        "closed": True,
        "self_stop_requested": True,
    }
    runtime.resume_sessions({}, "main")


def test_self_role_flags_do_not_expose_other_identities_and_follow_saved_handoff(tmp_path):
    client, backend, store, _, root, old, own = setup(tmp_path)
    new = host_create_session(client, headers=root, json={"command": ["true"]}).json()["session_id"]
    saved = store.load()["gateway"]
    saved["evolution"]["main_owner"] = new
    saved["evolution"]["transitions"] = [{"from": old, "to": new, "version_id": "tested"}]
    app = create_app(
        model_name="test/model",
        sessions={},
        main_owner=new,
        sandbox=backend,
        restored_state=saved,
        usage_log=tmp_path / "resumed-usage.jsonl",
        forward_model=lambda **kwargs: {"output": []},
        remote_call=lambda *args: {},
    )
    restored = TestClient(app)
    old_status = restored.get("/sessions/self", headers=own).json()
    new_status = restored.get(
        "/sessions/self", headers={"Authorization": "Bearer " + backend.tokens[new]}
    ).json()
    assert old_status["is_main"] is False and old_status["has_been_main"] is True
    assert new_status["is_main"] is True and new_status["has_been_main"] is True
    assert new not in json.dumps(old_status) and old not in json.dumps(new_status)
