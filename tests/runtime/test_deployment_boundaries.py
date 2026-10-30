import base64

import pytest
from fastapi.testclient import TestClient

from runtime.local.host_gateway import create_app


class SandboxBackend:
    def __init__(self):
        self.calls = []

    def create(self, owner, command, files, token):
        self.calls.append((owner, command, files, token))
        return {"session_id": "child-1", "status": "created"}

    def status(self, owner, session_id):
        self.calls.append((owner, session_id))
        return {"status": "completed"}


def app_client(tmp_path, **options):
    return TestClient(
        create_app(
            model_name="provider/model",
            sessions=options.pop("identities", {"parent-token": "parent"}),
            forward_model=lambda **kwargs: {"output": []},
            remote_call=options.pop("remote", lambda operation, payload: payload),
            usage_log=tmp_path / "usage.jsonl",
            **options,
        )
    )


def test_child_execution_gets_its_own_scope_without_exposing_token_to_parent(tmp_path):
    backend = SandboxBackend()
    client = app_client(tmp_path, sandbox=backend)
    response = client.post(
        "/sessions",
        headers={"Authorization": "Bearer parent-token"},
        json={"command": ["python", "candidate.py"], "files": {"candidate.py": "print('OK')"}},
    )
    assert response.status_code == 200
    child_owner, command, files, child_token = backend.calls[0]
    assert child_owner != "parent"
    assert child_token not in response.text
    assert child_token != "parent-token"
    assert command == ["python", "candidate.py"]
    assert files == {"candidate.py": "print('OK')"}
    submitted = client.post(
        "/jobs",
        headers={"Authorization": f"Bearer {child_token}"},
        json={"command": ["true"]},
    )
    assert submitted.json()["experiment_id"] == child_owner
    denied = client.get("/sessions/child-1", headers={"Authorization": f"Bearer {child_token}"})
    assert denied.status_code == 403


def test_session_client_cannot_choose_mounts_owner_or_docker_options(tmp_path):
    client = app_client(tmp_path, sandbox=SandboxBackend())
    for extra in ({"mounts": ["/"]}, {"owner": "other"}, {"privileged": True}):
        response = client.post(
            "/sessions",
            headers={"Authorization": "Bearer parent-token"},
            json={"command": ["true"], **extra},
        )
        assert response.status_code == 422


def test_fixture_keeps_peer_identity_private_and_verifies_real_peer_job(tmp_path):
    from runtime.local.isolation_fixture import IsolationFixtures

    records = []
    content = {}

    def remote(operation, payload):
        records.append((operation, payload))
        if operation == "submit":
            content.update(payload["files"])
            return {"job_id": "peer-job", "status": "created"}
        if operation == "status":
            return {"status": "running"}
        if operation == "artifact":
            return {
                "encoding": "base64",
                "data": base64.b64encode(content["sentinel.txt"].encode()).decode(),
            }
        return {"status": "cancelled"}

    fixtures = IsolationFixtures(remote, tmp_path / "fixtures")
    public = fixtures.create("research-a")
    assert public["peer_job_id"] == "peer-job"
    peer_owner = records[0][1]["experiment_id"]
    assert peer_owner != "research-a"
    assert peer_owner not in str(public)
    assert "token" not in str(public)
    assert fixtures.verify("research-a", public["fixture_id"])["sentinel_unchanged"]
    with pytest.raises(PermissionError):
        fixtures.verify("unrelated", public["fixture_id"])
    fixtures.close()
    assert records[-1] == ("cancel", {"experiment_id": peer_owner, "job_id": "peer-job"})


def test_fixture_failure_is_not_reported_as_success(tmp_path):
    from runtime.local.isolation_fixture import IsolationFixtures

    fixtures = IsolationFixtures(lambda op, payload: {"status": "resource_busy"}, tmp_path)
    with pytest.raises(RuntimeError, match="resource"):
        fixtures.create("research-a")


def test_candidate_files_and_artifacts_cannot_escape_scope(tmp_path):
    from runtime.local.candidate_runtime import CandidateRuntime, write_candidate_files

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for files in ({"../outside": "bad"}, {"/outside": "bad"}):
        with pytest.raises(ValueError):
            write_candidate_files(workspace, files)
    (workspace / "link").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        write_candidate_files(workspace, {"link/outside": "bad"})
    write_candidate_files(workspace, {"valid/report.json": "OK"})
    assert (workspace / "valid/report.json").read_text() == "OK"
    runtime = CandidateRuntime(
        root=tmp_path, image="image", inputs=tmp_path, public_a3=tmp_path, endpoint="http://gateway"
    )
    with pytest.raises(PermissionError):
        runtime.artifact("other", "unknown", "valid/report.json")


def test_closing_candidate_revokes_its_gateway_identity(tmp_path):
    backend = SandboxBackend()
    backend.cancel = lambda owner, session: {"status": "closed"}
    client = app_client(tmp_path, sandbox=backend)
    parent = {"Authorization": "Bearer parent-token"}
    client.post("/sessions", headers=parent, json={"command": ["true"]})
    token = backend.calls[0][3]
    assert client.delete("/sessions/child-1", headers=parent).status_code == 200
    assert (
        client.post(
            "/jobs", headers={"Authorization": f"Bearer {token}"}, json={"command": ["true"]}
        ).status_code
        == 401
    )


class NestedSandboxBackend:
    def __init__(self):
        self.tokens = {}
        self.active = set()
        self.fail_once = None

    def create(self, owner, command, files, token):
        self.tokens[owner] = token
        self.active.add(owner)
        return {"session_id": owner, "status": "created"}

    def cancel(self, owner, session_id):
        if self.fail_once == session_id:
            self.fail_once = None
            raise RuntimeError("Temporary Docker failure")
        self.active.discard(session_id)
        return {"status": "closed"}

    def status(self, owner, session_id):
        return {"status": "running" if session_id in self.active else "closed"}


def test_parent_close_reclaims_descendants_remote_jobs_and_peer_fixtures(tmp_path):
    from runtime.local.isolation_fixture import IsolationFixtures

    calls = []

    def remote(operation, payload):
        calls.append((operation, payload))
        return {"job_id": "job-" + payload["experiment_id"], "status": "created"}

    backend = NestedSandboxBackend()
    fixtures = IsolationFixtures(remote, tmp_path / "fixtures")
    client = app_client(tmp_path, sandbox=backend, fixtures=fixtures, remote=remote)
    parent = {"Authorization": "Bearer parent-token"}
    first = client.post("/sessions", headers=parent, json={"command": ["true"]}).json()[
        "session_id"
    ]
    first_auth = {"Authorization": "Bearer " + backend.tokens[first]}
    second = client.post("/sessions", headers=first_auth, json={"command": ["true"]}).json()[
        "session_id"
    ]
    second_auth = {"Authorization": "Bearer " + backend.tokens[second]}
    client.post("/fixtures/isolation", headers=second_auth)
    client.post("/jobs", headers=second_auth, json={"command": ["true"]})
    assert client.delete("/sessions/" + first + "?cascade=true", headers=parent).status_code == 200
    assert not backend.active
    assert not fixtures.records
    assert any(
        operation == "cancel" and payload["experiment_id"] == second for operation, payload in calls
    )
    for auth in (first_auth, second_auth):
        assert client.post("/jobs", headers=auth, json={"command": ["true"]}).status_code == 401


def test_default_close_preserves_descendants_and_ancestor_can_supervise_them(tmp_path):
    backend = NestedSandboxBackend()
    client = app_client(tmp_path, sandbox=backend)
    root_auth = {"Authorization": "Bearer parent-token"}
    first = client.post("/sessions", headers=root_auth, json={"command": ["true"]}).json()[
        "session_id"
    ]
    first_auth = {"Authorization": "Bearer " + backend.tokens[first]}
    second = client.post("/sessions", headers=first_auth, json={"command": ["true"]}).json()[
        "session_id"
    ]
    response = client.delete("/sessions/" + first, headers=root_auth)
    assert response.status_code == 409
    assert backend.active == {first, second}
    assert client.get("/sessions/" + second, headers=root_auth).status_code == 200
    nodes = client.get("/sessions", headers=root_auth).json()["sessions"]
    assert {node["session_id"] for node in nodes} == {first, second}
    assert next(node for node in nodes if node["session_id"] == second)["parent"] == first
    assert (
        client.delete("/sessions/" + first + "?cascade=true", headers=root_auth).status_code == 200
    )
    assert not backend.active
    events = (tmp_path / "session-events.jsonl").read_text()
    assert first in events and second in events
    assert all(token not in events for token in backend.tokens.values())


def test_ancestor_supervision_does_not_grant_sibling_or_unrelated_access(tmp_path):
    backend = NestedSandboxBackend()
    client = app_client(
        tmp_path, sandbox=backend, identities={"parent-token": "parent", "other-token": "other"}
    )
    parent = {"Authorization": "Bearer parent-token"}
    first = client.post("/sessions", headers=parent, json={"command": ["true"]}).json()[
        "session_id"
    ]
    second = client.post("/sessions", headers=parent, json={"command": ["true"]}).json()[
        "session_id"
    ]
    for token, target in ((backend.tokens[first], second), ("other-token", first)):
        auth = {"Authorization": "Bearer " + token}
        assert client.get("/sessions/" + target, headers=auth).status_code == 403
        assert client.get("/sessions", headers=auth).json()["sessions"] == []


def test_shutdown_blocks_new_resources_and_keeps_failed_cleanup_retryable(tmp_path):
    backend = NestedSandboxBackend()
    client = app_client(tmp_path, sandbox=backend)
    auth = {"Authorization": "Bearer parent-token"}
    first = client.post("/sessions", headers=auth, json={"command": ["true"]}).json()["session_id"]
    second = client.post("/sessions", headers=auth, json={"command": ["true"]}).json()["session_id"]
    backend.fail_once = first
    client.app.state.begin_shutdown()
    for endpoint in ("/jobs", "/sessions", "/fixtures/isolation"):
        assert client.post(endpoint, headers=auth, json={"command": ["true"]}).status_code == 503
    with pytest.raises(RuntimeError):
        client.app.state.close_resources()
    assert second not in backend.active
    assert first in backend.active
    assert client.delete("/sessions/" + first, headers=auth).status_code == 200
    assert not backend.active


@pytest.mark.parametrize("execution_error", ["job timed out", "container start failed"])
def test_failed_job_execution_does_not_prevent_scope_cleanup(tmp_path, execution_error):
    from runtime.local.isolation_fixture import IsolationFixtures

    def remote(operation, payload):
        if operation == "submit":
            return {"job_id": "job-" + payload["experiment_id"], "status": "created"}
        return {"status": "failed", "error": execution_error}

    backend = NestedSandboxBackend()
    fixtures = IsolationFixtures(remote, tmp_path / "fixtures")
    client = app_client(tmp_path, sandbox=backend, fixtures=fixtures, remote=remote)
    parent = {"Authorization": "Bearer parent-token"}
    session = client.post("/sessions", headers=parent, json={"command": ["true"]}).json()[
        "session_id"
    ]
    auth = {"Authorization": "Bearer " + backend.tokens[session]}
    client.post("/jobs", headers=auth, json={"command": ["true"]})
    client.post("/fixtures/isolation", headers=auth)
    status = client.get("/jobs/any-job", headers=auth)
    assert status.status_code == 200
    assert status.json()["error"] == execution_error
    assert client.delete("/sessions/" + session, headers=parent).status_code == 200
    assert not fixtures.records
    assert not backend.active
