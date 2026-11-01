"""Only a main Agent can delegate sealed data to its explicitly created worker."""

from argparse import Namespace

import pytest
from fastapi.testclient import TestClient

from runtime.local.candidate_versions import CandidateVersions
from runtime.local.host_gateway import create_app
from runtime.remote import remote_jobs
from runtime.remote.readonly_exports import ReadonlyExports


class Sandbox:
    def __init__(self, versions, exports, identifier):
        self.versions, self.exports, self.identifier = versions, exports, identifier
        self.tokens = {}
        self.fail = False

    def create_version(self, owner, command, token, version_id):
        # The read grant must exist before this candidate can run any code.
        self.exports.data_path(owner, self.identifier)
        if self.fail:
            raise RuntimeError("controlled launch failure")
        self.tokens[owner] = token
        self.versions.checkout(owner, version_id, owner)
        return {"session_id": owner, "version_id": version_id}

    def status(self, owner, session):
        return {"status": "running"}

    def artifact(self, owner, session, path):
        return {"data": "fixture evidence"}


def setup(tmp_path):
    store = remote_jobs.JobStore(tmp_path / "remote")
    job = store.create("main", ["true"], False, {}, run_id="run-one")
    (store.outputs(job) / "weights.bin").write_bytes(b"\x00\xff" * 4096)
    exports = ReadonlyExports(store.root)
    record = exports.create(
        "main", "run-one", store.outputs(job), job["job_id"], "outputs", "weights.bin"
    )
    exports.seal(record["export_id"])
    versions = CandidateVersions(tmp_path / "versions")
    backend = Sandbox(versions, exports, record["export_id"])

    def remote(operation, request):
        try:
            return remote_jobs.export_operation(store, request, Namespace(operation=operation))
        except (PermissionError, ValueError) as error:
            return {"status": "error", "error": type(error).__name__}

    app = create_app(
        model_name="test",
        sessions={"main-token": "main", "other-token": "other"},
        main_owner="main",
        sandbox=backend,
        versions=versions,
        forward_model=lambda **kwargs: pytest.fail("No model calls in this test"),
        remote_call=remote,
        usage_log=tmp_path / "usage.jsonl",
    )
    return TestClient(app), backend, exports, record["export_id"]


def auth(token):
    return {"Authorization": "Bearer " + token}


def test_direct_candidate_receives_only_requested_export_and_can_forward_after_promotion(tmp_path):
    client, backend, exports, identifier = setup(tmp_path)
    main = auth("main-token")
    baseline = client.post("/versions", headers=main, json={"files": {"entry.py": "pass"}}).json()
    response = client.post(
        "/evolution/trials",
        headers=main,
        json={
            "baseline_version": baseline["version_id"],
            "checklist": "fixture",
            "command": ["true"],
            "files": {"entry.py": "pass"},
            "read_only_exports": [identifier],
        },
    )
    assert response.status_code == 200, response.text
    child = response.json()["session_id"]
    child_auth = auth(backend.tokens[child])
    assert (
        client.get("/jobs/exports", headers=child_auth).json()["exports"][0]["export_id"]
        == identifier
    )
    assert client.get(f"/jobs/exports/{identifier}", headers=auth("other-token")).status_code == 403
    assert (
        client.post(
            f"/jobs/exports/{identifier}/grants", headers=main, json={"session_id": "other"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/jobs/exports/{identifier}/grants", headers=child_auth, json={"session_id": "other"}
        ).status_code
        == 403
    )
    retained = client.post(
        f"/evolution/trials/{child}/decision",
        headers=main,
        json={"retain": True, "reason": "fixture", "evidence": ["result.json"]},
    )
    assert retained.status_code == 200
    next_child = client.post(
        "/sessions",
        headers=child_auth,
        json={
            "command": ["true"],
            "files": {"entry.py": "pass"},
            "read_only_exports": [identifier],
        },
    )
    assert next_child.status_code == 200, next_child.text
    next_id = next_child.json()["session_id"]
    exports.data_path(next_id, identifier)
    assert (
        client.delete(
            f"/jobs/exports/{identifier}/grants/{child}", headers=auth(backend.tokens[next_id])
        ).status_code
        == 403
    )
    exports.data_path(child, identifier)
    assert (
        client.post(
            f"/jobs/exports/{identifier}/grants", headers=main, json={"session_id": child}
        ).status_code
        == 403
    )


def test_failed_candidate_launch_rolls_back_its_export_access(tmp_path):
    client, backend, exports, identifier = setup(tmp_path)
    backend.fail = True
    response = client.post(
        "/sessions",
        headers=auth("main-token"),
        json={
            "command": ["true"],
            "files": {"entry.py": "pass"},
            "read_only_exports": [identifier],
        },
    )
    assert response.status_code == 409
    assert exports.load(identifier)["readers"] == ["main"]


def test_worker_can_explicitly_deliver_and_delete_its_own_binary_result(tmp_path, monkeypatch):
    client, backend, exports, identifier = setup(tmp_path)
    response = client.post(
        "/sessions",
        headers=auth("main-token"),
        json={
            "command": ["true"],
            "files": {"entry.py": "pass"},
            "read_only_exports": [identifier],
        },
    )
    child = response.json()["session_id"]
    source = tmp_path / "worker-result.bin"
    source.write_bytes(b"worker result")
    record = exports.create(child, "run-one", source, "child-job", "outputs", "worker-result.bin")
    exports.seal(record["export_id"])
    assert (
        client.get(f"/jobs/exports/{record['export_id']}", headers=auth("main-token")).status_code
        == 403
    )
    delivery = client.post(
        f"/jobs/exports/{record['export_id']}/grants",
        headers=auth(backend.tokens[child]),
        json={"session_id": "main"},
    )
    assert delivery.status_code == 200
    assert (
        client.get(f"/jobs/exports/{record['export_id']}", headers=auth("main-token")).status_code
        == 200
    )
    monkeypatch.setattr(remote_jobs, "active_jobs", lambda store, **kwargs: [])
    assert (
        client.delete(
            f"/jobs/exports/{record['export_id']}", headers=auth(backend.tokens[child])
        ).status_code
        == 200
    )
    assert source.read_bytes() == b"worker result"
