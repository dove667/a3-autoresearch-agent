"""All registered research identities share reads; mutations keep their owner checks."""

import base64

from fastapi.testclient import TestClient
from host_fixtures import host_create_session

from a3_research_agent.host.gateway.app import create_app


def test_same_run_peers_read_workspaces_jobs_and_versions_without_cancellation_rights(tmp_path):
    class Sandbox:
        def __init__(self):
            self.tokens = {}

        def create(self, owner, command, files, token):
            self.tokens[owner] = token
            return {"session_id": owner}

        def status(self, owner, session):
            return {"status": "running"}

        def artifact(self, owner, session, path):
            return {"data": base64.b64encode(b"shared research").decode()}

    sandbox = Sandbox()
    jobs = {}

    def remote(operation, payload):
        if operation == "submit":
            job = payload["experiment_id"] + "-job"
            jobs[job] = payload["experiment_id"]
            return {"job_id": job, "status": "running"}
        if jobs.get(payload["job_id"]) != payload["experiment_id"]:
            return {"status": "error", "error": "PermissionError"}
        return {"status": "running", "data": "evidence"}

    app = create_app(
        model_name="fixture",
        sessions={"root-token": "root"},
        sandbox=sandbox,
        main_owner="root",
        forward_model=lambda **kwargs: {},
        remote_call=remote,
        usage_log=tmp_path / "usage.jsonl",
    )
    with TestClient(app) as client:
        root = {"Authorization": "Bearer root-token"}
        first = host_create_session(client, headers=root, json={"command": ["true"]}).json()[
            "session_id"
        ]
        second = host_create_session(client, headers=root, json={"command": ["true"]}).json()[
            "session_id"
        ]
        a = {"Authorization": "Bearer " + sandbox.tokens[first]}
        b = {"Authorization": "Bearer " + sandbox.tokens[second]}
        job = client.post("/jobs", headers=a, json={"command": ["true"]}).json()["job_id"]
        assert (
            client.get(
                f"/sessions/{first}/artifacts?path=research/result.json", headers=b
            ).status_code
            == 200
        )
        assert client.get(f"/sessions/{first}", headers=b).status_code == 200
        assert len(client.get("/sessions", headers=b).json()["sessions"]) == 2
        assert client.get(f"/jobs/{job}", headers=b).status_code == 200
        assert client.get(f"/jobs/{job}/artifacts?path=result.json", headers=b).status_code == 200
        assert client.delete(f"/jobs/{job}", headers=b).status_code == 403
        assert client.delete(f"/sessions/{first}", headers=b).status_code == 403
        assert (
            client.get("/sessions/not-in-this-run/artifacts?path=x", headers=b).status_code == 403
        )


def test_bootstrap_identity_cannot_bypass_single_agent_rule(tmp_path):
    app = create_app(
        model_name="fixture",
        sessions={"token": "bootstrap"},
        forward_model=lambda **kwargs: {},
        remote_call=lambda *args: {},
        usage_log=tmp_path / "usage.jsonl",
    )
    response = TestClient(app).post(
        "/sessions",
        headers={"Authorization": "Bearer token"},
        json={"command": ["true"]},
    )
    assert response.status_code == 403
