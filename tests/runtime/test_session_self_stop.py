from fastapi.testclient import TestClient
from host_fixtures import host_create_session

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.runs.state import RunStateStore


class SessionBackend:
    def check_capacity(self):
        pass

    def __init__(self):
        self.tokens = {}
        self.records = {}
        self.fail_stop = False

    def create(self, owner, command, files, token, *, inputs=None):
        self.tokens[owner] = token
        self.records[owner] = {"status": "running"}
        return {"session_id": owner, "status": "created"}

    def status(self, owner, session):
        assert owner == session
        return {"session_id": session, **self.records[session]}

    def stop(self, owner, session):
        assert owner == session
        if self.fail_stop:
            raise RuntimeError("controlled process failure")
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
        session_runtime=backend,
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


def test_gateway_recovery_reconciles_stop_completed_before_gateway_saved(tmp_path):
    _, backend, store, _, root, session, own = setup(tmp_path)
    saved = store.load()["gateway"]
    # Native runtime saved the stop, but the Host died before revoking the token.
    backend.records[session] = {"status": "exited", "self_stopped": True}
    recovered = TestClient(
        create_app(
            model_name="test/model",
            sessions={},
            main_owner="root",
            session_runtime=backend,
            usage_log=tmp_path / "usage.jsonl",
            state_store=store,
            restored_state=saved,
            forward_model=lambda **kwargs: {"output": []},
            remote_call=lambda *args: {},
        )
    )
    assert recovered.post("/v1/responses", headers=own, json={"input": "again"}).status_code == 401
    assert recovered.get(f"/sessions/{session}", headers=root).json()["status"] == "exited"
