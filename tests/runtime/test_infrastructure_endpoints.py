import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from host_fixtures import host_create_session

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.sessions.containers import CandidateCapacityError
from a3_research_agent.host.sessions.versions import CandidateVersions


class VersionSandbox:
    def __init__(self, versions):
        self.versions = versions
        self.launched = []

    def create_version(self, owner, command, token, version_id):
        workspace = self.versions.checkout(owner, version_id, owner)
        self.launched.append((owner, token, workspace))
        return {"session_id": owner, "status": "created", "version_id": version_id}


def test_host_starts_large_local_package_while_agent_uploads_remain_bounded(tmp_path):
    versions = CandidateVersions(tmp_path / "versions")
    backend = VersionSandbox(versions)
    app = create_app(
        model_name="provider/model",
        sessions={"root-token": "root"},
        forward_model=lambda **kw: {},
        remote_call=lambda *args: {},
        usage_log=tmp_path / "usage.jsonl",
        versions=versions,
        sandbox=backend,
        main_owner="root",
        start_main_session=True,
    )
    files = {f"file_{index}.txt": "fixture" for index in range(129)}
    with TestClient(app) as client:
        upload = host_create_session(
            client,
            headers={"Authorization": "Bearer root-token"},
            json={"command": ["true"], "files": files},
        )
        assert upload.status_code >= 400
        session = app.state.start_local_harness(["true"], files, "Bearer root-token")
        child, token, workspace = backend.launched[-1]
        assert session["session_id"] == child
        assert len(list(workspace.iterdir())) == 129
        assert (
            client.get("/evolution", headers={"Authorization": "Bearer root-token"}).json()[
                "main_owner"
            ]
            == child
        )
        with pytest.raises(HTTPException) as error:
            app.state.start_local_harness(["true"], files, "Bearer " + token)
        assert error.value.status_code == 403


def test_saved_version_has_distinct_execution_identity_and_shared_readers(tmp_path):
    versions = CandidateVersions(tmp_path / "versions")
    backend = VersionSandbox(versions)
    client = TestClient(
        create_app(
            model_name="provider/model",
            sessions={"parent-token": "parent", "other-token": "other"},
            forward_model=lambda **kw: {},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
            versions=versions,
            sandbox=backend,
        )
    )
    parent = {"Authorization": "Bearer parent-token"}
    item = client.post("/versions", headers=parent, json={"files": {"entry.py": "pass"}}).json()
    response = host_create_session(
        client,
        headers=parent,
        json={
            "command": ["python", "entry.py"],
            "version_id": item["version_id"],
        },
    )
    assert response.status_code == 200
    child, token, workspace = backend.launched[0]
    assert child != "parent" and token != "parent-token"
    assert token not in response.text
    assert (workspace / "entry.py").read_text() == "pass"
    child_auth = {"Authorization": "Bearer " + token}
    assert client.get("/versions", headers=child_auth).json()["versions"] == [item]
    other = {"Authorization": "Bearer other-token"}
    assert client.get("/versions", headers=other).json()["versions"] == [item]
    assert (
        client.post(
            "/sessions",
            headers=other,
            json={
                "command": ["true"],
                "version_id": item["version_id"],
            },
        ).status_code
        == 403
    )


def test_diagnostic_log_correlates_errors_without_saving_payload_or_exception(tmp_path):
    def failing(**kwargs):
        raise RuntimeError("PRIVATE_PROVIDER_SECRET")

    client = TestClient(
        create_app(
            model_name="provider/model",
            sessions={"PRIVATE_AUTH_TOKEN": "a"},
            forward_model=failing,
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
        )
    )
    response = client.post(
        "/v1/responses",
        json={"input": "PRIVATE_PROMPT"},
        headers={"Authorization": "Bearer PRIVATE_AUTH_TOKEN"},
    )
    assert response.status_code == 502
    text = (tmp_path / "gateway-events.jsonl").read_text()
    events = [json.loads(line) for line in text.splitlines()]
    assert [event["event"] for event in events] == ["started", "finished"]
    assert events[-1]["request_id"] == response.headers["X-Request-ID"]
    assert events[-1]["error_layer"] == "model_provider"
    assert events[-1]["error_type"] == "RuntimeError"
    assert events[-1]["status_code"] == 502
    assert all(
        secret not in text + response.text
        for secret in (
            "PRIVATE_PROVIDER_SECRET",
            "PRIVATE_AUTH_TOKEN",
            "PRIVATE_PROMPT",
        )
    )


def test_busy_host_session_returns_actionable_retry(tmp_path):
    class BusySandbox:
        def create(self, *args):
            raise CandidateCapacityError("private detail")

    client = TestClient(
        create_app(
            model_name="provider/model",
            sessions={"token": "a"},
            forward_model=lambda **kw: {},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
            sandbox=BusySandbox(),
        )
    )
    response = host_create_session(
        client, headers={"Authorization": "Bearer token"}, json={"command": ["true"]}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["max_active"] == 2
    assert response.json()["detail"]["retry_after_seconds"] == 2


def test_comparison_check_requires_identity_and_returns_condition_differences(tmp_path):
    client = TestClient(
        create_app(
            model_name="provider/model",
            sessions={"token": "a"},
            forward_model=lambda **kw: {},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
        )
    )
    assert (
        client.post("/checks/comparison", json={"baseline": {}, "candidate": {}}).status_code == 401
    )
    response = client.post(
        "/checks/comparison",
        headers={"Authorization": "Bearer token"},
        json={"baseline": {}, "candidate": {}},
    )
    assert response.status_code == 200
    assert not response.json()["comparable"]


def test_descendant_can_save_and_ancestor_can_collect_new_versions(tmp_path):
    versions = CandidateVersions(tmp_path / "versions")
    backend = VersionSandbox(versions)
    client = TestClient(
        create_app(
            model_name="provider/model",
            sessions={"token": "parent"},
            forward_model=lambda **kw: {},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
            versions=versions,
            sandbox=backend,
        )
    )
    parent = {"Authorization": "Bearer token"}
    first = host_create_session(
        client,
        headers=parent,
        json={
            "command": ["true"],
            "files": {"entry.py": "pass"},
        },
    ).json()
    child, token, _ = backend.launched[0]
    child_auth = {"Authorization": "Bearer " + token}
    saved = client.post(
        "/versions",
        headers=child_auth,
        json={
            "parent_version": first["version_id"],
            "files": {"entry.py": "print('next')"},
        },
    ).json()
    assert saved["version_id"] in str(client.get("/versions", headers=parent).json())
    collected = client.get("/sessions/" + child + "/versions", headers=parent)
    assert saved in collected.json()["versions"]
    next_session = host_create_session(
        client,
        headers=parent,
        json={
            "command": ["true"],
            "version_id": saved["version_id"],
        },
    )
    assert next_session.status_code == 200
    assert (backend.launched[-1][2] / "entry.py").read_text() == "print('next')"


def test_session_without_explicit_parent_inherits_callers_starting_version(tmp_path):
    versions = CandidateVersions(tmp_path / "versions")
    backend = VersionSandbox(versions)
    client = TestClient(
        create_app(
            model_name="provider/model",
            sessions={"token": "parent"},
            forward_model=lambda **kw: {},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
            versions=versions,
            sandbox=backend,
        )
    )
    first = host_create_session(
        client,
        headers={"Authorization": "Bearer token"},
        json={
            "command": ["true"],
            "files": {"entry.py": "pass"},
        },
    ).json()
    child = {"Authorization": "Bearer " + backend.launched[0][1]}
    second = host_create_session(
        client,
        headers=child,
        json={
            "command": ["true"],
            "files": {"entry.py": "print('next')"},
        },
    ).json()
    assert versions.list(backend.launched[0][0])[-1]["parent_version"] == first["version_id"]
    assert second["version_id"] != first["version_id"]


def test_only_initial_root_session_receives_private_previous_run_inputs(tmp_path):
    class PrivateInputSandbox(VersionSandbox):
        def __init__(self, versions):
            super().__init__(versions)
            self.private_receivers = []

        def create_initial(self, owner, command, token, version_id, inputs):
            self.private_receivers.append(owner)
            return self.create_version(owner, command, token, version_id)

    versions = CandidateVersions(tmp_path / "versions")
    backend = PrivateInputSandbox(versions)
    client = TestClient(
        create_app(
            model_name="provider/model",
            sessions={"token": "parent"},
            forward_model=lambda **kw: {},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
            versions=versions,
            sandbox=backend,
            initial_inputs=tmp_path / "private-inputs",
        )
    )
    parent = {"Authorization": "Bearer token"}
    initial = host_create_session(
        client, headers=parent, json={"command": ["true"], "files": {}}
    ).json()
    child = {"Authorization": "Bearer " + backend.launched[0][1]}
    host_create_session(client, headers=child, json={"command": ["true"], "files": {}})
    host_create_session(client, headers=parent, json={"command": ["true"], "files": {}})
    assert backend.private_receivers == [initial["session_id"]]
