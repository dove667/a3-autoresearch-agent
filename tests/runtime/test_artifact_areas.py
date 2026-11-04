import base64

import pytest
from test_host_gateway import client_for
from test_remote_jobs import load_runtime


def test_outputs_requires_explicit_area_and_keeps_owner_boundary(tmp_path):
    m = load_runtime()
    store = m.JobStore(tmp_path)
    j = store.create("a", ["true"], False, {}, run_id="run-one")
    (store.outputs(j) / "result.json").write_bytes(b"output")
    request = {"experiment_id": "a", "job_id": j["job_id"], "path": "result.json"}
    with pytest.raises(FileNotFoundError):
        m.read_artifact(store, request)
    result = m.read_artifact(store, {**request, "area": "outputs"})
    assert base64.b64decode(result["data"]) == b"output"
    assert result["area"] == "outputs"
    with pytest.raises(PermissionError):
        m.read_artifact(store, {**request, "area": "outputs", "experiment_id": "b"})


@pytest.mark.parametrize("path", ["../private", "/outputs/result.json", "link"])
def test_outputs_rejects_escapes_and_symlinks(tmp_path, path):
    m = load_runtime()
    store = m.JobStore(tmp_path)
    j = store.create("a", ["true"], False, {}, run_id="run-one")
    (tmp_path / "private").write_text("SECRET")
    (store.outputs(j) / "link").symlink_to(tmp_path / "private")
    with pytest.raises(ValueError):
        m.read_artifact(
            store, {"experiment_id": "a", "job_id": j["job_id"], "path": path, "area": "outputs"}
        )


def test_outputs_does_not_guess_a_root_for_legacy_jobs(tmp_path):
    m = load_runtime()
    store = m.JobStore(tmp_path)
    j = store.create("a", ["true"], False, {})
    with pytest.raises(ValueError, match="legacy"):
        m.read_artifact(
            store, {"experiment_id": "a", "job_id": j["job_id"], "path": "x", "area": "outputs"}
        )


def test_host_forwards_explicit_area_and_returns_safe_missing_hint(tmp_path):
    calls = []

    def remote(op, payload):
        calls.append(payload)
        return {"status": "error", "error": "FileNotFoundError", "message": "SECRET /private/token"}

    api = client_for(tmp_path, remote=remote)
    headers = {"Authorization": "Bearer token-a"}
    response = api.get(
        "/jobs/j/artifacts", params={"path": "x", "area": "outputs"}, headers=headers
    )
    assert calls[-1]["area"] == "outputs"
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "artifact_not_found"
    assert "SECRET" not in response.text
    assert (
        api.get(
            "/jobs/j/artifacts", params={"path": "x", "area": "../../"}, headers=headers
        ).status_code
        == 422
    )


def test_mixed_controller_version_cannot_silently_return_workspace_as_outputs(tmp_path):
    api = client_for(
        tmp_path, remote=lambda *a: {"encoding": "base64", "data": "d29ya3NwYWNl", "path": "x"}
    )
    result = api.get(
        "/jobs/j/artifacts",
        params={"path": "x", "area": "outputs"},
        headers={"Authorization": "Bearer token-a"},
    )
    assert result.status_code == 502


def test_outputs_preserves_size_ceiling_and_symlink_parent_rejection(tmp_path):
    m = load_runtime()
    store = m.JobStore(tmp_path)
    j = store.create("a", ["true"], False, {}, run_id="run-one")
    (store.outputs(j) / "large").write_bytes(b"123")
    with pytest.raises(ValueError, match="size"):
        m.read_artifact(
            store,
            {
                "experiment_id": "a",
                "job_id": j["job_id"],
                "path": "large",
                "area": "outputs",
                "max_bytes": 2,
            },
        )
    other = tmp_path / "other"
    other.mkdir()
    (other / "data").write_text("SECRET")
    (store.outputs(j) / "folder").symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError):
        m.read_artifact(
            store,
            {"experiment_id": "a", "job_id": j["job_id"], "path": "folder/data", "area": "outputs"},
        )
