"""Current session artifacts preserve exact bytes with bounded reads."""

import base64

import pytest

from a3_research_agent.host.sessions.workspace import ArtifactTooLargeError, read_artifact


def test_large_trace_can_be_read_in_bounded_byte_pages(tmp_path):
    with (tmp_path / "trajectory.jsonl").open("wb") as stream:
        stream.seek(32 * 1024 * 1024)
        stream.write(b"last-result")
    page = read_artifact(tmp_path, "trajectory.jsonl", 32 * 1024 * 1024 - 2, 16)
    assert base64.b64decode(page["data"]) == b"\0\0last-result"
    assert page["offset"] == 33554430
    assert page["next_offset"] == page["size_bytes"] == 33554443
    assert page["eof"] is True


def test_large_unpaged_artifact_requires_explicit_page(tmp_path):
    with (tmp_path / "trajectory.jsonl").open("wb") as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    with pytest.raises(ArtifactTooLargeError) as caught:
        read_artifact(tmp_path, "trajectory.jsonl")
    assert caught.value.size_bytes == 33554433


def test_paged_read_reassembles_bytes_and_observes_append(tmp_path):
    path = tmp_path / "trace.json"
    path.write_bytes(b"abcDEFghi")
    chunks, offset = [], 0
    for _ in range(3):
        page = read_artifact(tmp_path, "trace.json", offset, 4)
        chunks.append(base64.b64decode(page["data"]))
        offset = page["next_offset"]
    assert b"".join(chunks) == b"abcDEFghi"
    with path.open("ab") as stream:
        stream.write(b"NEW")
    page = read_artifact(tmp_path, "trace.json", offset, 4)
    assert base64.b64decode(page["data"]) == b"NEW"
    assert page["size_bytes"] == page["next_offset"] == 12


@pytest.mark.parametrize("offset,limit", [(-1, 1), (0, 0), (0, 33554433), (6, 1)])
def test_invalid_page_bounds_are_rejected(tmp_path, offset, limit):
    (tmp_path / "small.txt").write_text("hello")
    with pytest.raises(ValueError):
        read_artifact(tmp_path, "small.txt", offset, limit)


def test_artifact_path_cannot_escape_workspace(tmp_path):
    (tmp_path / "link.txt").symlink_to(tmp_path.parent / "private.txt")
    for name in ("../private.txt", "/private.txt", "link.txt"):
        with pytest.raises(ValueError):
            read_artifact(tmp_path, name, 0, 2)


def test_small_artifact_returns_exact_encoded_bytes(tmp_path):
    (tmp_path / "small.txt").write_bytes(b"hello")
    assert read_artifact(tmp_path, "small.txt") == {
        "path": "small.txt",
        "encoding": "base64",
        "data": "aGVsbG8=",
    }
