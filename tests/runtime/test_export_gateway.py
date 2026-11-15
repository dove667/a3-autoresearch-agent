"""Host startup grants selected sealed assets before native execution."""

from argparse import Namespace

import pytest
from fastapi.testclient import TestClient
from host_fixtures import host_create_session

from a3_research_agent.execution.controller import store as job_store
from a3_research_agent.execution.exports import operations as job_exports
from a3_research_agent.execution.exports.readonly import ReadonlyExports
from a3_research_agent.host.gateway.app import create_app


class Sandbox:
    def __init__(self, exports, identifier):
        self.exports, self.identifier = exports, identifier
        self.tokens = {}
        self.fail = False

    def check_capacity(self):
        pass

    def create(self, owner, command, files, token, *, inputs=None):
        # The read grant must exist before this candidate can run any code.
        self.exports.data_path(owner, self.identifier)
        if self.fail:
            raise RuntimeError("controlled launch failure")
        self.tokens[owner] = token
        return {"session_id": owner, "status": "created"}

    def status(self, owner, session):
        return {"status": "running"}

    def artifact(self, owner, session, path):
        return {"data": "fixture evidence"}


def setup(tmp_path):
    store = job_store.JobStore(tmp_path / "remote")
    job = store.create("main", ["true"], False, {}, run_id="run-one")
    (store.outputs(job) / "weights.bin").write_bytes(b"\x00\xff" * 4096)
    exports = ReadonlyExports(store.root)
    record = exports.create(
        "main", "run-one", store.outputs(job), job["job_id"], "outputs", "weights.bin"
    )
    exports.seal(record["export_id"])
    backend = Sandbox(exports, record["export_id"])

    def remote(operation, request):
        try:
            return job_exports.export_operation(store, request, Namespace(operation=operation))
        except (PermissionError, ValueError) as error:
            return {"status": "error", "error": type(error).__name__}

    app = create_app(
        model_name="test",
        sessions={"main-token": "main", "other-token": "other"},
        main_owner="main",
        session_runtime=backend,
        forward_model=lambda **kwargs: pytest.fail("No model calls in this test"),
        remote_call=remote,
        usage_log=tmp_path / "usage.jsonl",
    )
    return TestClient(app), backend, exports, record["export_id"]


def auth(token):
    return {"Authorization": "Bearer " + token}


def test_host_start_grants_only_selected_export_before_process_starts(tmp_path):
    client, backend, exports, identifier = setup(tmp_path)
    result = host_create_session(
        client,
        headers=auth("main-token"),
        json={"command": ["true"], "files": {}, "read_only_exports": [identifier]},
    )
    assert result.status_code == 200
    session = result.json()["session_id"]
    exports.data_path(session, identifier)
    own = auth(backend.tokens[session])
    assert client.get(f"/jobs/exports/{identifier}", headers=own).status_code == 200
    assert client.post("/sessions", headers=own, json={"command": ["true"]}).status_code == 405
    assert (
        client.post(
            f"/jobs/exports/{identifier}/grants", headers=own, json={"session_id": "outsider"}
        ).status_code
        == 403
    )


def test_failed_candidate_launch_rolls_back_its_export_access(tmp_path):
    client, backend, exports, identifier = setup(tmp_path)
    backend.fail = True
    response = host_create_session(
        client,
        headers=auth("main-token"),
        json={
            "command": ["true"],
            "files": {"entry.py": "pass"},
            "read_only_exports": [identifier],
        },
    )
    assert response.status_code == 409
    assert exports.load(identifier)["readers"] == ["main"]
