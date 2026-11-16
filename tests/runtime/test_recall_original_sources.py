"""Recall retrieves actual observations beyond truncated previews without tool echoes."""

import hashlib
import io

import pytest

from a3_research_agent.harness import history, state_paths
from a3_research_agent.harness.context import manager, source_search


def record_result(result, *, name="job_artifact"):
    call = {"type": "function_call", "name": name, "call_id": "observation"}
    view = manager.bound_tool_result(result, call)
    history.append_history_many([call, manager.output_message(view, call["call_id"])])
    return view


def test_recall_finds_middle_of_large_original_with_readable_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    view = record_result({"report": "x" * 20000 + " 实验失败：解码器版本冲突 " + "y" * 20000})
    assert "解码器" not in view["preview"]
    result = history.recall_tool({"keywords": ["解码器"]})
    hit = result["items"][0]
    assert hit["source"]["path"] == view["path"]
    with source_search.verified_text(hit["source"]["path"], hit["source"]["sha256"]) as (stream, _):
        stream.read(hit["source"]["offset"])
        assert "解码器" in stream.read(hit["source"]["limit"])
    assert result["warnings"] == []


def test_recall_finds_decoded_utf8_artifact(tmp_path, monkeypatch):
    import base64

    monkeypatch.chdir(tmp_path)
    text = "头" * 20000 + "精确命中±0.125" + "尾" * 20000
    record_result({"encoding": "base64", "data": base64.b64encode(text.encode()).decode()})
    result = history.recall_tool({"keywords": ["精确命中"]})
    assert any(hit["source"]["path"].endswith(".txt") for hit in result["items"])
    assert result["warnings"] == []


def test_corrupt_original_is_reported_without_returning_unverified_matches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    view = record_result({"report": "x" * 20000 + "hidden_conclusion" + "y" * 20000})
    state_paths.path(view["path"]).write_text("hidden_conclusion")
    result = history.recall_tool({"keywords": ["hidden_conclusion"]})
    assert result["items"] == []
    assert result["warnings"][0]["error"] == "source_unavailable_or_unverified"


@pytest.mark.parametrize("mode", ["evidence", "raw"])
def test_recall_never_searches_archived_recall_echoes(tmp_path, monkeypatch, mode):
    monkeypatch.chdir(tmp_path)
    record_result({"past_hit": "evidence_echo " * 10000}, name="recall")
    assert history.recall_tool({"keywords": ["evidence_echo"], "mode": mode})["items"] == []


def test_source_symlink_cannot_escape_state_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    view = record_result({"report": "x" * 20000 + "hidden_conclusion" + "y" * 20000})
    outside = tmp_path / "outside.json"
    original = state_paths.path(view["path"])
    outside.write_bytes(original.read_bytes())
    original.unlink()
    original.symlink_to(outside)
    result = history.recall_tool({"keywords": ["hidden_conclusion"]})
    assert not result["items"] and result["warnings"]


def test_scanner_matches_across_chunk_boundaries_and_returns_character_offsets(monkeypatch):
    import re

    monkeypatch.setattr(source_search, "CHUNK_CHARACTERS", 2048)
    text = "中" * 2045 + "目标结论" + "文" * 2200
    found = source_search.find_text(io.StringIO(text), re.compile("目标结论"))
    assert "目标结论" in found["snippet"]
    assert text[found["offset"] : found["offset"] + len(found["snippet"])] == found["snippet"]


def test_artifact_invalid_reference_reports_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    call = {"type": "function_call", "name": "job_artifact", "call_id": "a"}
    original = manager.output_message("x", "a")
    reference = manager.archive(original, "tool-output")
    view = {
        "archive_version": 1,
        "preview_complete": False,
        "path": reference,
        "artifact": {"path": "../other-run/secret.txt", "encoding": "utf-8", "sha256": "a" * 64},
    }
    history.append_history_many([call, manager.output_message(view, "a")])
    result = history.recall_tool({"keywords": ["secret"]})
    assert result["warnings"][0]["error"] == "invalid_artifact_reference"


def test_verified_text_detects_modified_source_after_scanning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = state_paths.path("source.txt")
    path.parent.mkdir(parents=True)
    path.write_text("valid")
    with (
        pytest.raises(ValueError, match="source_changed"),
        source_search.verified_text("source.txt", hashlib.sha256(b"valid").hexdigest()) as (
            stream,
            _,
        ),
    ):
        assert stream.read() == "valid"
        path.write_text("changed")
