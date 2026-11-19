"""Artifact listing is additive: old deployments degrade to an actionable hint."""

import hashlib
import json

import pytest
from test_host_gateway import client_for

from a3_research_agent.execution.controller import artifacts as job_artifacts
from a3_research_agent.execution.controller import store as job_store
from a3_research_agent.harness import job_outputs, job_tools, tool_results

JOB = "a" * 32
MISSING = json.dumps({"detail": {"code": "artifact_not_found", "area": "outputs"}})


def page(content="hello"):
    return {
        "encoding": "utf-8",
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "bytes": len(content.encode()),
        "unit": "characters",
        "offset": 0,
        "end": len(content),
        "total": len(content),
        "eof": True,
        "content": content,
    }


def test_listing_returns_top_level_names_and_filters_traversal():
    requests = []

    def request(method, path):
        requests.append((method, path))
        return {
            "area": "outputs",
            "entries": ["seed-10000.json", {"name": "diag"}, "../escape", "a/b"],
        }

    assert job_outputs.listing(request, JOB) == {"entries": ["seed-10000.json", "diag"]}
    assert requests == [("GET", f"/jobs/{JOB}/artifacts/list?area=outputs")]


def test_directory_can_be_listed_directly_or_discovered_from_a_file_read():
    calls = []

    def request(method, path):
        calls.append(path)
        if "/artifacts/list?" in path:
            return {"area": "outputs", "directory": "diag/seed1", "entries": ["events.json"]}
        return {
            "http_error": 400,
            "body": json.dumps({"detail": {"code": "artifact_is_directory"}}),
        }

    for field in ("directory", "path"):
        result = job_outputs.read_batch(
            {"job_id": JOB, "area": "outputs", field: "diag/seed1"}, request
        )
        assert result["directory"] == "diag/seed1"
        assert result["entries"] == ["events.json"]
    assert calls[0].endswith("area=outputs&directory=diag%2Fseed1")


def test_controller_lists_nested_artifact_directory_without_following_symlinks(tmp_path):
    store = job_store.JobStore(tmp_path)
    job = store.create("owner", ["true"], False, {}, run_id="run-one")
    root = store.outputs(job)
    (root / "diag" / "seed1").mkdir(parents=True)
    (root / "diag" / "seed1" / "events.json").write_text("{}")
    (root / "link").symlink_to(root / "diag", target_is_directory=True)
    request = {"experiment_id": "owner", "job_id": job["job_id"], "area": "outputs"}
    result = job_artifacts.list_artifacts(store, request | {"directory": "diag/seed1"})
    assert result["directory"] == "diag/seed1"
    assert result["entries"] == ["events.json"]
    with pytest.raises(IsADirectoryError):
        job_artifacts.read_artifact(store, request | {"path": "diag/seed1"})
    with pytest.raises(ValueError):
        job_artifacts.list_artifacts(store, request | {"directory": "link/seed1"})


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        ({"http_error": 404}, "http_404"),
        ({"http_error": 502}, "http_502"),
        ({"status": "error", "error": "ValueError"}, "controller_error"),
        ({"area": "outputs"}, "listing_unsupported"),
        ("not-a-dict", "invalid_response"),
    ],
)
def test_listing_degrades_without_raising(response, reason):
    assert job_outputs.listing(lambda *args: response, JOB) == {"unavailable": reason}


def test_listing_catches_transport_errors():
    def request(*args):
        raise ConnectionResetError("lost")

    assert job_outputs.listing(request, JOB) == {"unavailable": "transport_error"}
    assert job_outputs.listing(lambda *args: {}, "not-a-job") == {"unavailable": "invalid_job_id"}


def test_artifact_not_found_receipt_lists_the_actual_outputs():
    calls = []

    def request(method, path):
        calls.append((method, path))
        if path.endswith("/artifacts/list?area=outputs"):
            return {"area": "outputs", "entries": ["seed-10000.json", "diag"]}
        return {"http_error": 400, "body": MISSING}

    result = tool_results.normalize(
        job_tools.dispatch(
            "job_artifact",
            {"job_id": JOB, "path": "seed-99999.json", "area": "outputs"},
            request,
        )
    )
    assert result["ok"] is False
    assert result["error"] == "artifact_not_found"
    assert result["outputs"] == ["seed-10000.json", "diag"]
    assert "job_list" in result["hint"]
    assert "seed-10000.json" in result["hint"]
    assert "maintainer" not in result["hint"]
    assert [call[0] for call in calls] == ["GET", "GET"]


def test_old_deployment_listing_failure_keeps_the_hint_and_omits_the_listing():
    calls = []

    def request(method, path):
        calls.append((method, path))
        if path.endswith("/artifacts/list?area=outputs"):
            return {"http_error": 404}
        return {"http_error": 400, "body": MISSING}

    result = job_tools.dispatch(
        "job_artifact",
        {"job_id": JOB, "path": "seed-10000.json", "area": "outputs"},
        request,
    )
    assert result["error"] == "artifact_not_found"
    assert "outputs" not in result
    assert "job_list" in result["hint"] and "top-level" in result["hint"]
    assert len(calls) == 2


def test_artifact_receipt_uses_entries_already_present_in_the_error_detail():
    detail = {
        "detail": {
            "code": "artifact_not_found",
            "area": "outputs",
            "entries": ["seed-10000.json"],
        }
    }

    def request(method, path):
        return {"http_error": 400, "body": json.dumps(detail)}

    result = job_tools.dispatch(
        "job_artifact", {"job_id": JOB, "path": "missing.json", "area": "outputs"}, request
    )
    assert result["outputs"] == ["seed-10000.json"]
    assert "seed-10000.json" in result["hint"]


def test_single_artifact_read_keeps_its_existing_shape():
    result = tool_results.normalize(
        job_tools.dispatch(
            "job_artifact",
            {"job_id": JOB, "path": "a.txt", "area": "outputs"},
            lambda method, path: page("hello"),
        )
    )
    assert result["ok"] is True
    assert result["job_id"] == JOB and result["area"] == "outputs" and result["path"] == "a.txt"
    assert result["content"] == "hello" and result["total"] == 5


def test_multi_path_artifact_read_returns_every_item():
    def request(method, path):
        assert "path=b.txt" not in path or "path=a.txt" not in path
        return page("a" if "path=a.txt" in path else "b")

    result = tool_results.normalize(
        job_tools.dispatch(
            "job_artifact",
            {"job_id": JOB, "paths": ["a.txt", "b.txt"], "area": "outputs"},
            request,
        )
    )
    assert result["ok"] is True
    assert result["count"] == 2 and result["failed"] == 0
    assert [item["content"] for item in result["items"]] == ["a", "b"]
    assert result["items"][0]["path"] == "a.txt"


def test_multi_path_artifact_read_attaches_one_listing_when_all_missing():
    calls = []

    def request(method, path):
        calls.append(path)
        if path.endswith("/artifacts/list?area=outputs"):
            return {"area": "outputs", "entries": ["seed-10000.json"]}
        return {"http_error": 400, "body": MISSING}

    result = job_tools.dispatch(
        "job_artifact",
        {"job_id": JOB, "paths": ["a.txt", "b.txt"], "area": "outputs"},
        request,
    )
    assert result["error"] == "artifact_batch_failed"
    assert result["failed"] == 2
    assert result["outputs"] == ["seed-10000.json"]
    assert sum(path.endswith("/artifacts/list?area=outputs") for path in calls) == 1


def test_artifact_rejects_path_and_paths_together():
    result = job_tools.dispatch(
        "job_artifact",
        {"job_id": JOB, "path": "a.txt", "paths": ["b.txt"], "area": "outputs"},
        lambda *args: page(),
    )
    assert result["error"] == "invalid_artifact_paths"


def test_controller_listing_reads_top_level_outputs(tmp_path):
    store = job_store.JobStore(tmp_path)
    job = store.create("a", ["true"], False, {}, run_id="run-one")
    (store.outputs(job) / "seed-10000.json").write_text("{}")
    (store.outputs(job) / "diag").mkdir()
    result = job_artifacts.list_artifacts(
        store, {"experiment_id": "a", "job_id": job["job_id"], "area": "outputs"}
    )
    assert result == {
        "area": "outputs",
        "entries": ["diag", "seed-10000.json"],
        "count": 2,
        "truncated": False,
    }


def test_controller_listing_keeps_legacy_jobs_and_missing_dirs_explicit(tmp_path):
    store = job_store.JobStore(tmp_path)
    legacy = store.create("a", ["true"], False, {})
    with pytest.raises(ValueError, match="legacy"):
        job_artifacts.list_artifacts(
            store, {"experiment_id": "a", "job_id": legacy["job_id"], "area": "outputs"}
        )
    current = store.create("a", ["true"], False, {}, run_id="run-two")
    (store.outputs(current) / "one.json").unlink(missing_ok=True)
    result = job_artifacts.list_artifacts(
        store, {"experiment_id": "a", "job_id": current["job_id"], "area": "outputs"}
    )
    assert result["entries"] == []


def test_gateway_exposes_the_new_listing_route(tmp_path):
    calls = []

    def remote(operation, payload):
        calls.append((operation, payload))
        return {"area": "outputs", "entries": ["seed-10000.json", "diag"]}

    client = client_for(tmp_path, remote=remote)
    response = client.get(
        f"/jobs/{JOB}/artifacts/list",
        params={"area": "outputs"},
        headers={"Authorization": "Bearer token-a"},
    )
    assert response.status_code == 200
    assert response.json()["entries"] == ["seed-10000.json", "diag"]
    assert calls == [
        ("artifacts-list", {"experiment_id": "experiment-a", "job_id": JOB, "area": "outputs"})
    ]


def test_gateway_listing_rejects_an_unconfirmed_controller_response(tmp_path):
    client = client_for(tmp_path, remote=lambda *args: {"entries": ["seed.json"]})
    response = client.get(
        f"/jobs/{JOB}/artifacts/list",
        params={"area": "outputs"},
        headers={"Authorization": "Bearer token-a"},
    )
    assert response.status_code == 502
