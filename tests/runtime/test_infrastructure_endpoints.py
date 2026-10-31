import json

from fastapi.testclient import TestClient

from runtime.local.candidate_runtime import CandidateCapacityError
from runtime.local.candidate_versions import CandidateVersions
from runtime.local.host_gateway import create_app


class VersionSandbox:
    def __init__(self, versions):
        self.versions = versions
        self.launched = []

    def create_version(self, owner, command, token, version_id):
        workspace = self.versions.checkout(owner, version_id, owner)
        self.launched.append((owner, token, workspace))
        return {"session_id": owner, "status": "created", "version_id": version_id}


def test_saved_version_runs_under_new_identity_and_siblings_cannot_read_it(tmp_path):
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
    response = client.post(
        "/sessions",
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
    assert client.get("/versions", headers=other).json()["versions"] == []
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


def test_busy_candidate_returns_actionable_retry_and_logged_reason(tmp_path):
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
    response = client.post(
        "/sessions", headers={"Authorization": "Bearer token"}, json={"command": ["true"]}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["retry_after_seconds"] == 2
    assert "candidate_capacity" in (tmp_path / "gateway-events.jsonl").read_text()


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
    first = client.post(
        "/sessions",
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
    assert saved["version_id"] not in str(client.get("/versions", headers=parent).json())
    collected = client.get("/sessions/" + child + "/versions", headers=parent)
    assert saved in collected.json()["versions"]
    next_session = client.post(
        "/sessions",
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
    first = client.post(
        "/sessions",
        headers={"Authorization": "Bearer token"},
        json={
            "command": ["true"],
            "files": {"entry.py": "pass"},
        },
    ).json()
    child = {"Authorization": "Bearer " + backend.launched[0][1]}
    second = client.post(
        "/sessions",
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
    initial = client.post(
        "/sessions", headers=parent, json={"command": ["true"], "files": {}}
    ).json()
    child = {"Authorization": "Bearer " + backend.launched[0][1]}
    client.post("/sessions", headers=child, json={"command": ["true"], "files": {}})
    client.post("/sessions", headers=parent, json={"command": ["true"], "files": {}})
    assert backend.private_receivers == [initial["session_id"]]
