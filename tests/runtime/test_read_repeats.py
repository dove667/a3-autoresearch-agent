"""A page already in context is not carried a second time."""

import json

import pytest


@pytest.fixture(autouse=True)
def native_roots(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("A3_WORKSPACE", str(tmp_path))
    for name, variable in (("inputs", "A3_INPUT_ROOT"), ("reference", "A3_REFERENCE_ROOT")):
        directory = tmp_path.parent / f"{name}-{tmp_path.name}"
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))


def read_page(path="notes.txt", **extra):
    from a3_research_agent.harness import file_tools

    return file_tools.dispatch("read", {"path": path, **extra})


def deliver(page, call_id="c1"):
    """Simulate the model receiving this page: the harness marks it delivered."""
    from a3_research_agent.harness import operational_state

    operational_state.record_page(call_id, page)
    operational_state.mark_delivered(
        [{"type": "function_call_output", "call_id": call_id, "output": json.dumps(page)}]
    )


def test_a_second_read_of_the_same_page_is_shortened(tmp_path):
    (tmp_path / "notes.txt").write_text("复现要点 " * 500)
    first = read_page()
    assert first["content"] and not first.get("repeated")
    deliver(first)

    second = read_page()
    assert second["repeated"] is True
    assert second["content"] == ""
    assert second["sha256"] == first["sha256"]
    assert second["next_offset"] == first["next_offset"]
    assert "recall" in second["hint"]


def test_a_different_page_still_returns_text(tmp_path):
    (tmp_path / "notes.txt").write_text("x" * 9000)
    first = read_page(limit=4000)
    deliver(first)
    second = read_page(offset=4000, limit=4000)
    assert second["content"] and not second.get("repeated")


def test_text_comes_back_after_compaction_rewrites_history(tmp_path):
    from a3_research_agent.harness import operational_state

    (tmp_path / "notes.txt").write_text("记录 " * 500)
    first = read_page()
    deliver(first)
    assert read_page()["repeated"] is True

    operational_state.reset_delivered()
    after = read_page()
    assert after["content"] and not after.get("repeated")


def test_an_unchanged_file_with_new_content_is_not_shortened(tmp_path):
    (tmp_path / "notes.txt").write_text("first " * 500)
    first = read_page()
    deliver(first)
    (tmp_path / "notes.txt").write_text("second " * 500)
    fresh = read_page()
    assert fresh["sha256"] != first["sha256"]
    assert "second" in fresh["content"]
