import base64

import pytest
from fastapi.testclient import TestClient

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.sessions.containers import CandidateRuntime


@pytest.fixture
def artifact_session(tmp_path, monkeypatch):
    sandbox = CandidateRuntime(
        root=tmp_path / "candidates",
        image="controlled/image",
        inputs=tmp_path / "inputs",
        public_a3=tmp_path / "a3",
        endpoint="http://controlled",
    )
    monkeypatch.setattr(sandbox, "docker", lambda *args: "container" if args[0] == "create" else "")
    client = TestClient(
        create_app(
            model_name="controlled/model",
            sessions={"root-token": "root", "peer-token": "peer"},
            forward_model=lambda **kwargs: {},
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
            sandbox=sandbox,
        )
    )
    headers = {"Authorization": "Bearer root-token"}
    response = client.post("/sessions", headers=headers, json={"command": ["true"]})
    response.raise_for_status()
    session_id = response.json()["session_id"]
    yield (
        client,
        headers,
        f"/sessions/{session_id}/artifacts",
        sandbox.root / session_id / "workspace",
    )


def test_large_trace_can_be_read_in_bounded_byte_pages(artifact_session):
    client, headers, url, workspace = artifact_session
    with (workspace / "trajectory.jsonl").open("wb") as stream:
        stream.seek(32 * 1024 * 1024)
        stream.write(b"last-result")
    response = client.get(
        url,
        headers=headers,
        params={"path": "trajectory.jsonl", "offset": 32 * 1024 * 1024 - 2, "limit": 16},
    )
    assert response.status_code == 200
    page = response.json()
    assert base64.b64decode(page["data"]) == b"\0\0last-result"
    assert page["offset"] == 33554430
    assert page["next_offset"] == 33554443
    assert page["size_bytes"] == 33554443
    assert page["eof"] is True


def test_large_unpaged_read_explains_how_to_request_a_page(artifact_session):
    client, headers, url, workspace = artifact_session
    with (workspace / "trajectory.jsonl").open("wb") as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    response = client.get(url, headers=headers, params={"path": "trajectory.jsonl"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["reason"] == "artifact_too_large"
    assert detail["size_bytes"] == 33554433
    assert detail["max_bytes"] == 33554432
    assert detail["retry_query"] == {"offset": 0, "limit": 1048576}


def test_paged_read_reassembles_bytes_and_tracks_appended_data(artifact_session):
    client, headers, url, workspace = artifact_session
    path = workspace / "trace.json"
    path.write_bytes(b"abcDEFghi")
    chunks = []
    offset = 0
    for _ in range(3):
        page = client.get(
            url, headers=headers, params={"path": "trace.json", "offset": offset, "limit": 4}
        ).json()
        chunks.append(base64.b64decode(page["data"]))
        offset = page["next_offset"]
    assert b"".join(chunks) == b"abcDEFghi"
    assert offset == 9 and page["eof"]
    with path.open("ab") as stream:
        stream.write(b"NEW")
    page = client.get(
        url, headers=headers, params={"path": "trace.json", "offset": offset, "limit": 4}
    ).json()
    assert base64.b64decode(page["data"]) == b"NEW"
    assert page["size_bytes"] == page["next_offset"] == 12


@pytest.mark.parametrize("query", [{"offset": -1}, {"limit": 0}, {"limit": 33554433}])
def test_invalid_page_bounds_are_rejected(artifact_session, query):
    client, headers, url, workspace = artifact_session
    (workspace / "small.txt").write_text("hello")
    response = client.get(url, headers=headers, params={"path": "small.txt", **query})
    assert response.status_code == 422


def test_pagination_preserves_owner_and_file_boundaries(artifact_session, tmp_path):
    client, headers, url, workspace = artifact_session
    (workspace / "small.txt").write_text("hello")
    (tmp_path / "private.txt").write_text("private")
    (workspace / "link.txt").symlink_to(tmp_path / "private.txt")
    query = {"path": "small.txt", "limit": 2}
    assert (
        client.get(url, headers={"Authorization": "Bearer peer-token"}, params=query).status_code
        == 403
    )
    for name in ("../private.txt", "/private.txt", "link.txt"):
        assert client.get(url, headers=headers, params={**query, "path": name}).status_code == 422
    response = client.get(url, headers=headers, params={**query, "offset": 6})
    assert response.status_code == 422


def test_small_unpaged_artifact_keeps_existing_response_shape(artifact_session):
    client, headers, url, workspace = artifact_session
    (workspace / "small.txt").write_bytes(b"hello")
    response = client.get(url, headers=headers, params={"path": "small.txt"})
    assert response.json() == {"path": "small.txt", "encoding": "base64", "data": "aGVsbG8="}
