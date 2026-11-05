"""Caller-owned source references compose immutable snapshots without inline uploads."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.sessions.containers import CandidateRuntime
from a3_research_agent.host.sessions.versions import CandidateVersions


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def assembly(tmp_path, monkeypatch):
    versions = CandidateVersions(tmp_path / "versions")
    base = versions.create(
        "caller", {"dependency.py": "VALUE = 7\n", "research/input.json": '{"x":7}'}
    )
    sandbox = CandidateRuntime(
        root=tmp_path / "candidates",
        image="fixture",
        inputs=tmp_path / "inputs",
        public_a3=tmp_path / "public",
        endpoint="http://fixture",
        versions=versions,
    )
    workspace = sandbox.root / "caller/workspace"
    (workspace / "research").mkdir(parents=True)
    (workspace / "research/runner.py").write_text("from dependency import VALUE\nprint(VALUE)\n")
    (workspace / "research/prereg.json").write_text('{"before_execution":true}')
    sandbox.records["caller"] = {
        "owner": "caller",
        "session_id": "caller",
        "closed": False,
        "container_removed": True,
        "terminal_state": {"Status": "running"},
    }
    docker = []

    def fake_docker(*args, **kwargs):
        docker.append(args)
        return "fixture-container" if args[0] == "create" else ""

    monkeypatch.setattr(sandbox, "docker", fake_docker)
    restored = {
        "sessions": {"caller-token": "caller", "other-token": "other"},
        "root_owners": ["root"],
        "children": {"caller": {"owner": "caller", "parent": "root", "closed": False}},
        "scope_parents": {"caller": "root"},
        "jobs": {},
        "evolution": {"main_owner": "caller", "trials": {}, "transitions": []},
    }
    client = TestClient(
        create_app(
            model_name="fixture/no-model",
            main_owner="caller",
            sessions={},
            sandbox=sandbox,
            versions=versions,
            restored_state=restored,
            forward_model=lambda **kw: pytest.fail("No model"),
            remote_call=lambda *a: pytest.fail("No remote call"),
            usage_log=tmp_path / "usage.jsonl",
        )
    )
    refs = [
        {"path": name, "sha256": sha((workspace / name).read_bytes())}
        for name in ["research/runner.py", "research/prereg.json"]
    ]
    body = {
        "command": ["python", "research/runner.py"],
        "version_id": base["version_id"],
        "workspace_files": refs,
    }
    return client, versions, sandbox, workspace, base, body, docker


def post(client, body, token="caller-token"):
    return client.post("/sessions", headers={"Authorization": "Bearer " + token}, json=body)


def test_snapshot_and_workspace_references_produce_complete_new_version(assembly):
    client, versions, _sandbox, workspace, base, body, docker = assembly
    response = post(client, body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["version_id"] != base["version_id"]
    snapshot = versions.read_snapshot(versions.root, result["version_id"])
    assert snapshot == {
        "dependency.py": "VALUE = 7\n",
        "research/input.json": '{"x":7}',
        **{r["path"]: (workspace / r["path"]).read_text() for r in body["workspace_files"]},
    }
    assert versions.read_snapshot(versions.root, base["version_id"]) == {
        "dependency.py": "VALUE = 7\n",
        "research/input.json": '{"x":7}',
    }
    receipt = result["assembly"]
    assert receipt["base_version_id"] == base["version_id"]
    assert {x["path"]: x["sha256"] for x in receipt["workspace_files"]} == {
        x["path"]: x["sha256"] for x in body["workspace_files"]
    }
    assert versions.records[result["version_id"]]["parent_version"] == base["version_id"]
    assert sum(x[0] == "create" for x in docker) == 1
    assert "caller-token" not in response.text


@pytest.mark.parametrize(
    "case",
    [
        "other_base",
        "other_actor",
        "missing_base",
        "mixed_files",
        "mixed_parent",
        "absolute",
        "traversal",
        "alias",
        "control",
        "not_research",
        "duplicate",
        "wrong_hash",
        "missing",
        "symlink_leaf",
        "symlink_parent",
        "invalid_utf8",
        "oversize",
        "too_many",
    ],
)
def test_invalid_assembly_fails_before_creating_a_session(assembly, tmp_path, case):
    client, versions, _sandbox, workspace, base, body, docker = assembly
    if case == "other_base":
        body["version_id"] = versions.create("other", {"private.py": "private"})["version_id"]
    elif case == "other_actor":
        response = post(client, body, "other-token")
        assert response.status_code == 403
        assert not docker
        return
    elif case == "missing_base":
        del body["version_id"]
    elif case == "mixed_files":
        body["files"] = {"research/new.py": "pass"}
    elif case == "mixed_parent":
        body["parent_version"] = base["version_id"]
    elif case in {"absolute", "traversal", "alias", "control", "not_research"}:
        body["workspace_files"][0]["path"] = {
            "absolute": "/etc/passwd",
            "traversal": "research/../../outside",
            "alias": "research/./runner.py",
            "control": "research/.env",
            "not_research": "checkpoint.json",
        }[case]
    elif case == "duplicate":
        body["workspace_files"].append(body["workspace_files"][0])
    elif case == "wrong_hash":
        body["workspace_files"][0]["sha256"] = "0" * 64
    elif case == "missing":
        (workspace / "research/runner.py").unlink()
    elif case == "symlink_leaf":
        path = workspace / "research/runner.py"
        path.unlink()
        path.symlink_to(workspace / "research/prereg.json")
    elif case == "symlink_parent":
        path = workspace / "research"
        path.rename(workspace / "elsewhere")
        path.symlink_to(workspace / "elsewhere", target_is_directory=True)
    elif case == "invalid_utf8":
        (workspace / "research/runner.py").write_bytes(b"\xff")
        body["workspace_files"][0]["sha256"] = sha(b"\xff")
    elif case == "oversize":
        (workspace / "research/runner.py").write_bytes(b"x" * (1024 * 1024 + 1))
    else:
        body["workspace_files"] *= 9
    before = set(versions.records)
    response = post(client, body)
    assert response.status_code in {403, 422}, response.text
    assert not docker
    assert set(versions.records) == before


def test_attachment_hashes_are_bound_to_bytes_read_not_live_paths(assembly):
    client, _versions, _sandbox, workspace, _base, body, docker = assembly
    (workspace / "research/runner.py").write_text("changed after hash")
    assert post(client, body).status_code == 422
    assert not docker


def test_complete_assembled_snapshot_retains_upload_bounds(assembly, monkeypatch):
    import a3_research_agent.host.sessions.versions as version_module

    client, _versions, _sandbox, _workspace, _base, body, docker = assembly
    monkeypatch.setattr(version_module, "MAX_INPUT_FILES", 3)
    assert post(client, body).status_code == 422
    assert not docker


def test_explicit_attachment_replacement_preserves_original_snapshot(assembly):
    client, versions, _sandbox, workspace, base, body, _docker = assembly
    path = "research/input.json"
    (workspace / path).write_text('{"x":8}')
    body["workspace_files"] = [{"path": path, "sha256": sha((workspace / path).read_bytes())}]
    response = post(client, body)
    assert response.status_code == 200, response.text
    assert versions.read_snapshot(versions.root, response.json()["version_id"])[path] == '{"x":8}'
    assert versions.read_snapshot(versions.root, base["version_id"])[path] == '{"x":7}'


def test_file_directory_conflict_fails_before_deriving_version(assembly):
    client, versions, _sandbox, _workspace, _base, body, docker = assembly
    body["version_id"] = versions.create("caller", {"research": "file, not directory"})[
        "version_id"
    ]
    before = set(versions.records)
    assert post(client, body).status_code == 422
    assert set(versions.records) == before and not docker


def test_assembly_is_not_implicitly_enabled_for_formal_trials(assembly):
    client, _versions, _sandbox, _workspace, base, body, docker = assembly
    response = client.post(
        "/evolution/trials",
        headers={"Authorization": "Bearer caller-token"},
        json={**body, "baseline_version": base["version_id"], "checklist": "fixture"},
    )
    assert response.status_code == 422, response.text
    assert not docker


def test_reference_cannot_choose_another_source_owner(assembly):
    client, _versions, _sandbox, _workspace, _base, body, docker = assembly
    body["workspace_files"][0]["owner"] = "other"
    assert post(client, body).status_code == 422
    assert not docker


def test_combined_byte_limit_is_checked_without_saving_version(assembly, monkeypatch):
    import a3_research_agent.host.sessions.versions as version_module

    client, versions, _sandbox, _workspace, _base, body, docker = assembly
    monkeypatch.setattr(version_module, "MAX_INPUT_BYTES", 30)
    before = set(versions.records)
    assert post(client, body).status_code == 422
    assert not docker and set(versions.records) == before


def test_job_materializes_owned_sources_without_child_or_export_redelegation(assembly):
    client, _, _sandbox, workspace, _, body, docker = assembly
    runtime = client.app.state.save_checkpoint.__self__
    calls = []
    runtime.remote_call = lambda op, payload: calls.append((op, payload)) or {"job_id": "job-1"}
    collected = "orchestration/results/task/research/adapter.py"
    (workspace / collected).parent.mkdir(parents=True)
    (workspace / collected).write_bytes(b"VALUE = 9\r\n")
    refs = body["workspace_files"][:1] + [
        {"path": collected, "sha256": sha(b"VALUE = 9\r\n"), "destination": "adapter.py"}
    ]
    response = client.post(
        "/jobs",
        headers={"Authorization": "Bearer caller-token"},
        json={
            "command": ["python", "research/runner.py"],
            "workspace_files": refs,
            "input_exports": ["export-existing"],
        },
    )
    assert response.status_code == 200, response.text
    assert calls == [
        (
            "submit",
            {
                "command": ["python", "research/runner.py"],
                "gpu": False,
                "files": {
                    "research/runner.py": (workspace / "research/runner.py").read_text(),
                    "adapter.py": "VALUE = 9\r\n",
                },
                "input_exports": ["export-existing"],
                "experiment_id": "caller",
            },
        )
    ]
    assert response.json()["source_files"][1] == {
        "path": collected,
        "destination": "adapter.py",
        "sha256": sha(b"VALUE = 9\r\n"),
        "bytes": 11,
    }
    assert runtime.jobs["caller"] == {"job-1"}
    assert not docker


@pytest.mark.parametrize(
    "case",
    [
        "hash",
        "sibling",
        "secret",
        "symlink",
        "destination",
        "collision",
        "prefix",
        "count",
        "root_identity",
    ],
)
def test_job_reference_validation_precedes_remote_submission(assembly, case):
    client, _, _, workspace, _, body, _ = assembly
    runtime = client.app.state.save_checkpoint.__self__
    calls = []
    runtime.remote_call = lambda *args: calls.append(args) or {"job_id": "unexpected"}
    ref = dict(body["workspace_files"][0])
    payload = {"command": ["true"], "workspace_files": [ref]}
    token = "caller-token"
    if case == "hash":
        ref["sha256"] = "0" * 64
    elif case == "sibling":
        token = "other-token"
    elif case == "secret":
        ref["path"] = ".env"
    elif case == "symlink":
        p = workspace / ref["path"]
        p.unlink()
        p.symlink_to(workspace / "research/prereg.json")
    elif case == "destination":
        ref["destination"] = "../escaped.py"
    elif case == "collision":
        payload["files"] = {ref["path"]: "overwrite"}
    elif case == "prefix":
        payload["files"] = {"research": "blocks directory"}
    elif case == "count":
        payload["workspace_files"] *= 17
    elif case == "root_identity":
        runtime.sessions["root-token"] = "root"
        token = "root-token"
    response = client.post("/jobs", headers={"Authorization": "Bearer " + token}, json=payload)
    assert response.status_code in {403, 422}, response.text
    assert not calls
