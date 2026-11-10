from fastapi.testclient import TestClient

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.runs.state import RunStateStore
from a3_research_agent.host.sessions.versions import CandidateVersions


class Backend:
    def __init__(self):
        self.tokens = {}

    def create_version(self, owner, command, token, version):
        self.tokens[owner] = token
        return {"session_id": owner, "version_id": version}

    def status(self, owner, session):
        return {"status": "running"}

    def artifact(self, owner, session, path):
        return {"data": "evidence"}


def test_gateway_restart_preserves_main_trials_tokens_and_job_owner(tmp_path):
    store = RunStateStore(tmp_path)
    versions = CandidateVersions(tmp_path / "versions")
    backend = Backend()
    calls = []

    def remote(operation, payload):
        calls.append((operation, payload))
        return {"job_id": "job-1", "status": "running"}

    def build(restored=None):
        return create_app(
            model_name="provider/model",
            sessions={"root-token": "run-original"},
            main_owner="run-original",
            versions=versions,
            sandbox=backend,
            remote_call=remote,
            forward_model=lambda **kwargs: {"output": []},
            usage_log=tmp_path / "usage.jsonl",
            state_store=store,
            restored_state=restored,
        )

    app = build()
    client = TestClient(app)
    root = {"Authorization": "Bearer root-token"}
    baseline = versions.create("run-original", {"entry.py": "pass"})["version_id"]
    trial = client.post(
        "/evolution/trials",
        headers=root,
        json={
            "command": ["true"],
            "files": {"entry.py": "pass # new"},
            "baseline_version": baseline,
            "checklist": "test and wait",
        },
    ).json()["session_id"]
    client.post(
        f"/evolution/trials/{trial}/decision",
        headers=root,
        json={"retain": True, "reason": "tested", "evidence": ["result.json"]},
    )
    candidate = {"Authorization": "Bearer " + backend.tokens[trial]}
    client.post("/jobs", headers=candidate, json={"command": ["true"]})
    restored = TestClient(build(store.load()["gateway"]))
    assert restored.get("/evolution", headers=candidate).json()["main_owner"] == trial
    assert restored.get("/jobs/job-1", headers=candidate).status_code == 200
    assert calls[-1][1]["experiment_id"] == trial
    assert restored.get("/sessions", headers=root).json()["sessions"][0]["session_id"] == trial
    assert restored.get("/versions", headers=candidate).json()["versions"]
    assert (
        restored.get("/evolution", headers={"Authorization": "Bearer unknown"}).status_code == 401
    )
    assert store.load()["gateway"]["jobs"][trial] == ["job-1"]


def test_timeout_job_identity_survives_host_restart(tmp_path):
    store = RunStateStore(tmp_path)

    def remote(operation, payload):
        if operation == "submit":
            assert payload["job_id"] in store.load()["gateway"]["jobs"]["owner"]
            raise TimeoutError("response lost")
        return {"job_id": payload["job_id"], "status": "running"}

    def build(restored=None):
        return create_app(
            model_name="test/model",
            sessions={"token": "owner"},
            forward_model=lambda **kw: {},
            remote_call=remote,
            usage_log=tmp_path / "usage.jsonl",
            state_store=store,
            restored_state=restored,
        )

    headers = {"Authorization": "Bearer token"}
    response = TestClient(build()).post("/jobs", headers=headers, json={"command": ["true"]})
    identifier = response.json()["detail"]["job_id"]
    restored = TestClient(build(store.load()["gateway"]))
    assert store.load()["gateway"]["jobs"]["owner"] == [identifier]
    assert restored.get(f"/jobs/{identifier}", headers=headers).json()["status"] == "running"


def test_project_job_index_can_read_archived_scopes_without_legacy_run(tmp_path):
    from a3_research_agent.host.gateway.project_history import project_job_index

    run = tmp_path / "artifacts/rsi/run-current"
    run.mkdir(parents=True)
    index = run.parent.parent / "project-job-index.json"
    index.write_text(
        '{"scope_runs":{"archived":"run-archived","current":"run-current"},'
        '"job_scopes":{"old-job":"archived","current-job":"current"}}'
    )
    assert project_job_index(run) == ({"archived": "run-archived"}, {"old-job": "archived"})
