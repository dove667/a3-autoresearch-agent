from fastapi.testclient import TestClient

from runtime.local.candidate_versions import CandidateVersions
from runtime.local.host_gateway import create_app
from runtime.local.run_state import RunStateStore


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
