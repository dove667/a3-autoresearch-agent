"""Artifact pages cross the Controller boundary without whole-file payloads."""

import base64
import hashlib
import json
from contextlib import contextmanager

import pytest
from test_host_gateway import client_for

from a3_research_agent.execution.controller import artifacts
from a3_research_agent.execution.controller.store import JobStore


def artifact_request(store, content):
    job = store.create("experiment-a", ["true"], False, {}, run_id="run-one")
    (store.outputs(job) / "result.txt").write_bytes(content)
    return {
        "experiment_id": "experiment-a",
        "job_id": job["job_id"],
        "area": "outputs",
        "path": "result.txt",
    }


def test_unicode_pages_preserve_full_source_hash_and_legacy_base64(tmp_path):
    store = JobStore(tmp_path)
    text = "开始🙂\n" + "a" * 65530 + "结束界"
    raw = text.encode()
    request = artifact_request(store, raw)
    page = artifacts.read_artifact(store, {**request, "offset": 65533, "limit": 4})
    assert page == {
        "path": "result.txt",
        "area": "outputs",
        "encoding": "utf-8",
        "unit": "characters",
        "offset": 65533,
        "end": len(text),
        "total": len(text),
        "eof": True,
        "content": "a结束界",
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    beyond = artifacts.read_artifact(store, {**request, "offset": len(text) + 10, "limit": 4})
    assert beyond["content"] == "" and beyond["offset"] == beyond["end"] == len(text)
    legacy = artifacts.read_artifact(store, request)
    assert legacy == {
        "path": "result.txt",
        "area": "outputs",
        "encoding": "base64",
        "data": base64.b64encode(raw).decode(),
    }


def test_small_page_does_not_transfer_the_full_large_text(tmp_path):
    store = JobStore(tmp_path)
    raw = b"x" * (2 * 1024 * 1024)
    request = artifact_request(store, raw)
    page = artifacts.read_artifact(store, {**request, "offset": 100000, "limit": 9})
    assert page["content"] == "x" * 9
    assert page["bytes"] == page["total"] == len(raw)
    assert page["sha256"] == hashlib.sha256(raw).hexdigest()
    assert "data" not in page and len(json.dumps(page)) < 500


def test_multibyte_codepoints_split_across_stream_chunks_remain_intact(tmp_path):
    store = JobStore(tmp_path)
    request = artifact_request(store, ("a" * 65535 + "界🙂end").encode())
    page = artifacts.read_artifact(store, {**request, "offset": 65535, "limit": 2})
    assert page["content"] == "界🙂"
    assert page["offset"] == 65535 and page["end"] == 65537
    assert page["total"] == 65540 and not page["eof"]


def test_binary_page_returns_only_type_and_complete_hash_metadata(tmp_path):
    store = JobStore(tmp_path)
    raw = b"valid prefix" + b"\xff\x00" + b"large binary" * 100
    request = artifact_request(store, raw)
    result = artifacts.read_artifact(store, {**request, "offset": 0, "limit": 3})
    assert result == {
        "path": "result.txt",
        "area": "outputs",
        "encoding": "binary",
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


@pytest.mark.parametrize("raw,encoding", [(b"", "utf-8"), (b"truncated\xe7", "binary")])
def test_empty_text_and_incomplete_final_codepoint_are_classified(tmp_path, raw, encoding):
    store = JobStore(tmp_path)
    request = artifact_request(store, raw)
    result = artifacts.read_artifact(store, {**request, "offset": 0, "limit": 1})
    assert result["encoding"] == encoding
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    if encoding == "utf-8":
        assert result["content"] == "" and result["total"] == 0 and result["eof"]
    else:
        assert "content" not in result and "data" not in result


@pytest.mark.parametrize(
    "page",
    [
        {"offset": 0},
        {"limit": 1},
        {"offset": True, "limit": 1},
        {"offset": -1, "limit": 1},
        {"offset": 0, "limit": 0},
        {"offset": 0, "limit": 8001},
        {"offset": 0.0, "limit": 1},
    ],
)
def test_controller_rejects_incomplete_or_noninteger_pages(tmp_path, page):
    store = JobStore(tmp_path)
    request = artifact_request(store, b"text")
    with pytest.raises(ValueError, match="page"):
        artifacts.read_artifact(store, {**request, **page})


def test_pages_keep_owner_symlink_and_narrowed_size_boundaries(tmp_path):
    store = JobStore(tmp_path / "store")
    request = artifact_request(store, b"text")
    paged = {**request, "offset": 0, "limit": 2}
    with pytest.raises(PermissionError):
        artifacts.read_artifact(store, {**paged, "experiment_id": "other"})
    with pytest.raises(ValueError, match="size"):
        artifacts.read_artifact(store, {**paged, "max_bytes": 3})
    (store.outputs(store.load(request["job_id"], "experiment-a")) / "link").symlink_to(
        tmp_path / "private"
    )
    with pytest.raises(ValueError, match="symlink"):
        artifacts.read_artifact(store, {**paged, "path": "link"})


def test_page_scan_detects_a_file_changed_while_it_was_read(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    request = artifact_request(store, b"original")
    original_open = artifacts.open_source

    @contextmanager
    def changing_source(path):
        with original_open(path) as stream:

            class ChangingReader:
                def fileno(self):
                    return stream.fileno()

                def read(self, count):
                    data = stream.read(count)
                    if data:
                        path.write_bytes(b"changed and extended")
                    return data

            yield ChangingReader()

    monkeypatch.setattr(artifacts, "open_source", changing_source)
    with pytest.raises(ValueError, match="changed"):
        artifacts.read_artifact(store, {**request, "offset": 0, "limit": 3})


def test_gateway_forwards_pages_and_preserves_default_remote_contract(tmp_path):
    calls = []

    def remote(operation, payload):
        calls.append((operation, payload))
        return {"encoding": "utf-8", "area": "outputs", "content": "界"}

    api = client_for(tmp_path, remote=remote)
    headers = {"Authorization": "Bearer token-a"}
    response = api.get(
        "/jobs/j/artifacts",
        headers=headers,
        params={
            "path": "x",
            "area": "outputs",
            "offset": 2,
            "limit": 1,
            "max_bytes": 1000,
        },
    )
    assert response.status_code == 200
    assert calls[-1] == (
        "artifact",
        {
            "experiment_id": "experiment-a",
            "job_id": "j",
            "path": "x",
            "area": "outputs",
            "offset": 2,
            "limit": 1,
            "max_bytes": 1000,
        },
    )
    assert "data" not in response.json()
    api.get("/jobs/j/artifacts", headers=headers, params={"path": "x"})
    assert calls[-1][1] == {"experiment_id": "experiment-a", "job_id": "j", "path": "x"}


@pytest.mark.parametrize(
    "page",
    [
        {"offset": 0},
        {"limit": 2},
        {"offset": -1, "limit": 2},
        {"offset": "1.0", "limit": 2},
        {"offset": "true", "limit": 2},
        {"offset": 0, "limit": 8001},
        {"offset": 0, "limit": 0},
        {"offset": 0, "limit": 2, "max_bytes": 33554433},
    ],
)
def test_gateway_rejects_invalid_page_parameters_before_remote_call(tmp_path, page):
    calls = []
    api = client_for(tmp_path, remote=lambda *args: calls.append(args) or {})
    response = api.get(
        "/jobs/j/artifacts",
        headers={"Authorization": "Bearer token-a"},
        params={"path": "x", **page},
    )
    assert response.status_code == 422
    assert not calls


def test_gateway_does_not_present_an_old_wholefile_response_as_a_page(tmp_path):
    api = client_for(tmp_path, remote=lambda *args: {"encoding": "base64", "data": "YWJj"})
    response = api.get(
        "/jobs/j/artifacts",
        headers={"Authorization": "Bearer token-a"},
        params={"path": "x", "offset": 0, "limit": 1},
    )
    assert response.status_code == 502


def test_native_job_artifact_reads_controller_page_through_the_real_host(tmp_path):
    from a3_research_agent.harness import job_tools

    store = JobStore(tmp_path / "remote")
    raw = ("large observation\n" * 100000 + "结束🙂").encode()
    payload = artifact_request(store, raw)
    transfers = []

    def remote(operation, request):
        assert operation == "artifact"
        result = artifacts.read_artifact(store, request)
        transfers.append(result)
        return result

    api = client_for(tmp_path, remote=remote)

    def request(method, path, body=None):
        response = api.request(method, path, headers={"Authorization": "Bearer token-a"}, json=body)
        assert response.status_code == 200
        return response.json()

    result = job_tools.dispatch(
        "job_artifact",
        {
            "job_id": payload["job_id"],
            "area": "outputs",
            "path": "result.txt",
            "offset": 1800000,
            "limit": 3,
        },
        request,
    )
    assert result["content"] == "结束🙂" and result["eof"]
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["job_id"] == payload["job_id"]
    assert all("data" not in page and len(json.dumps(page)) < 500 for page in transfers)
