from fastapi.testclient import TestClient

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.runs.state import RunStateStore


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

    run = tmp_path / "trajectories/run-current"
    run.mkdir(parents=True)
    index = run.parent.parent / ".runtime/project-job-index.json"
    index.parent.mkdir()
    index.write_text(
        '{"scope_runs":{"archived":"run-archived","current":"run-current"},'
        '"job_scopes":{"old-job":"archived","current-job":"current"}}'
    )
    assert project_job_index(run) == ({"archived": "run-archived"}, {"old-job": "archived"})


def test_project_job_index_keeps_archived_run_identity_after_relocation(tmp_path):
    import json

    from native_run_support import native_run

    from a3_research_agent.host.gateway.project_history import project_job_index

    previous, _, state, _, _ = native_run(tmp_path, name="run-previous", actor="archived")
    relocated = previous.with_name("2026-11-09_21-00-00-000000")
    previous.rename(relocated)
    current = relocated.parent / "2026-11-10_21-00-00-000000"
    current.mkdir()
    (relocated / "model-usage.jsonl").write_text(json.dumps({"run_id": state["run_id"]}))

    scopes, jobs = project_job_index(current)
    assert scopes["archived"] == "run-previous"
    assert jobs == {"owned-job": "archived"}
