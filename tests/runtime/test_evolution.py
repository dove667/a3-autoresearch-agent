import base64

from fastapi.testclient import TestClient

from runtime.local.candidate_versions import CandidateVersions
from runtime.local.host_gateway import create_app


class Backend:
    def __init__(self):
        self.tokens = {}
        self.records = {}

    def create_version(self, owner, command, token, version):
        self.tokens[owner] = token
        self.records[owner] = {"status": "running", "report": False}
        return {"session_id": owner, "version_id": version, "status": "created"}

    def status(self, owner, session):
        return {"status": self.records[session]["status"]}

    def artifact(self, owner, session, path):
        if path != "result.json" or not self.records[session]["report"]:
            raise ValueError("Report missing")
        return {"data": base64.b64encode(b'{"result":"observed"}').decode()}

    def cancel(self, owner, session):
        self.records[session]["status"] = "exited"


def setup(tmp_path):
    versions = CandidateVersions(tmp_path / "versions")
    baseline = versions.create("root", {"entry.py": "print('old')"})
    backend = Backend()
    app = create_app(
        model_name="test/model",
        sessions={"root-token": "root"},
        main_owner="root",
        forward_model=lambda **kwargs: {},
        remote_call=lambda *args: {},
        usage_log=tmp_path / "usage.jsonl",
        sandbox=backend,
        versions=versions,
    )
    return TestClient(app), backend, baseline["version_id"]


def begin(client, baseline):
    response = client.post(
        "/evolution/trials",
        headers={"Authorization": "Bearer root-token"},
        json={
            "baseline_version": baseline,
            "checklist": "Recover a saved message and use it.",
            "command": ["python", "entry.py"],
            "files": {"entry.py": "print('new')"},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["session_id"]


def test_candidate_cannot_promote_itself_and_success_hands_off_same_process(tmp_path):
    client, backend, baseline = setup(tmp_path)
    session = begin(client, baseline)
    candidate = {"Authorization": "Bearer " + backend.tokens[session]}
    payload = {"retain": True, "reason": "Checklist met", "evidence": ["result.json"]}
    assert (
        client.post(
            f"/evolution/trials/{session}/decision", headers=candidate, json=payload
        ).status_code
        == 403
    )
    root = {"Authorization": "Bearer root-token"}
    assert (
        client.post(f"/evolution/trials/{session}/decision", headers=root, json=payload).status_code
        == 422
    )
    backend.records[session]["report"] = True
    response = client.post(f"/evolution/trials/{session}/decision", headers=root, json=payload)
    assert response.status_code == 200
    assert response.json()["main_owner"] == session
    assert backend.records[session]["status"] == "running"
    assert len(backend.tokens) == 1  # Promotion does not create another process.
    assert (
        client.post(
            "/evolution/trials",
            headers=root,
            json={
                "baseline_version": baseline,
                "checklist": "another",
                "command": ["true"],
                "files": {"entry.py": "pass"},
            },
        ).status_code
        == 403
    )


def test_reject_reclaims_candidate_and_keeps_old_main(tmp_path):
    client, backend, baseline = setup(tmp_path)
    session = begin(client, baseline)
    root = {"Authorization": "Bearer root-token"}
    response = client.post(
        f"/evolution/trials/{session}/decision",
        headers=root,
        json={"retain": False, "reason": "Failed checklist", "evidence": []},
    )
    assert response.status_code == 200
    assert response.json()["main_owner"] == "root"
    assert backend.records[session]["status"] == "exited"
    assert begin(client, baseline) != session


def test_only_one_pending_candidate_and_checklist_is_frozen(tmp_path):
    client, _backend, baseline = setup(tmp_path)
    session = begin(client, baseline)
    root = {"Authorization": "Bearer root-token"}
    response = client.post(
        "/evolution/trials",
        headers=root,
        json={
            "baseline_version": baseline,
            "checklist": "changed",
            "command": ["true"],
            "files": {"entry.py": "pass"},
        },
    )
    assert response.status_code == 409
    state = client.get("/evolution", headers=root).json()
    assert state["trials"][session]["checklist"] == "Recover a saved message and use it."


def test_promoted_main_can_start_next_generation_and_retired_main_cannot(tmp_path):
    client, backend, baseline = setup(tmp_path)
    session = begin(client, baseline)
    backend.records[session]["report"] = True
    root = {"Authorization": "Bearer root-token"}
    promoted = client.post(
        f"/evolution/trials/{session}/decision",
        headers=root,
        json={"retain": True, "reason": "Verified", "evidence": ["result.json"]},
    ).json()
    version = promoted["trials"][session]["candidate_version"]
    response = client.post(
        "/evolution/trials",
        headers={"Authorization": "Bearer " + backend.tokens[session]},
        json={
            "baseline_version": version,
            "checklist": "Next change",
            "command": ["python", "entry.py"],
            "files": {"entry.py": "print('generation 2')"},
        },
    )
    assert response.status_code == 200
    second = response.json()["session_id"]
    assert (
        client.post(
            f"/evolution/trials/{second}/decision",
            headers=root,
            json={"retain": False, "reason": "Old main must not decide", "evidence": []},
        ).status_code
        == 403
    )
