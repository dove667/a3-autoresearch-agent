"""Operational receipts never imply semantic consumption or research acceptance."""

import importlib.util
import json
import pathlib
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2] / "src/a3_research_agent/harness"


def load(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_file_location(
        "state_" + uuid.uuid4().hex, ROOT / "operational_state.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pages_report_hash_length_eof_and_preserve_unicode(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    p = tmp_path / "data"
    p.write_text("你好abcdef")
    first = m.read_page(p, 0, 2)
    last = m.read_page(p, 2, 8000)
    assert first["content"] + last["content"] == p.read_text()
    assert first["sha256"] == last["sha256"] and first["total"] == 8
    assert not first["eof"] and last["eof"]
    assert m.read_page(p, 999, 4)["eof"]
    p.write_text("你好changed")
    assert m.read_page(p, 0, 2)["sha256"] != first["sha256"]


def test_receipts_distinguish_retrieved_delivered_and_preview(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    p = tmp_path / "file"
    p.write_text("a" * 9000)
    page = m.read_page(p)
    m.record_page("c1", page)
    assert m.view()["reads"][0]["delivered"] == []
    m.mark_delivered(
        [
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": json.dumps({"preview": "partial", "receipt_id": "c1"}),
            }
        ]
    )
    assert m.view()["reads"][0]["delivered"] == []
    m.mark_delivered(
        [{"type": "function_call_output", "call_id": "c1", "output": json.dumps(page)}]
    )
    assert m.view()["reads"][0]["delivered"] == [[0, 4000]]
    m.record_page("c2", m.read_page(p))
    assert m.counters()["repeated_reads"] == 1
    assert m.counters()["delivered_reads"] == 1


def test_corrupt_state_fails_closed_and_snapshot_remains(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    m.state_file().parent.mkdir()
    m.state_file().write_text("broken")
    with pytest.raises(ValueError):
        m.view()
    assert m.state_file().read_text() == "broken"


@pytest.mark.parametrize("args", [{"offset": -1}, {"limit": 0}, {"limit": True}, {"limit": 9000}])
def test_invalid_pages_fail_explicitly(tmp_path, monkeypatch, args):
    m = load(tmp_path, monkeypatch)
    p = tmp_path / "x"
    p.write_text("data")
    with pytest.raises(ValueError):
        m.read_page(p, **args)


def test_repeated_cycles_keep_exact_coverage_without_manual_task_ledger(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    p = tmp_path / "report"
    p.write_text("内容" * 20000)
    for i in range(30):
        page = m.read_page(p, i * 1000, 1000)
        m.record_page(str(i), page)
        m.mark_delivered(
            [{"type": "function_call_output", "call_id": str(i), "output": json.dumps(page)}]
        )
        m = load(tmp_path, monkeypatch)
    assert m.view()["reads"][0]["delivered"] == [[0, 30000]]
    assert "items" not in m.view()
    assert len(json.dumps(m.view(), ensure_ascii=False).encode()) <= 8000


def test_sparse_coverage_is_bounded_and_never_fills_gaps(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    p = tmp_path / "sparse"
    p.write_text("a" * 1000)
    for i in range(140):
        m.record_page(str(i), m.read_page(p, i * 2, 1))
    entry = next(iter(m.load()["reads"].values()))
    assert len(entry["retrieved"]) <= 64 and entry["coverage_incomplete"]
    assert all(b - a == 1 for a, b in entry["retrieved"])


@pytest.mark.parametrize("diagnostic_fails", [False, True])
def test_receipt_failure_cannot_hide_successful_job_result(tmp_path, monkeypatch, diagnostic_fails):
    from a3_research_agent.harness import agent

    result = {"job_id": "a" * 32, "status": "created"}

    def fail(*args, **kwargs):
        raise OSError("receipt failed")

    monkeypatch.setattr(agent, "req", lambda *args, **kwargs: result)
    monkeypatch.setattr(agent.operational_state, "record_gateway", fail)
    monkeypatch.setattr(agent, "log", fail if diagnostic_fails else lambda value: None)
    assert agent.request_host("POST", "/jobs", {}) == result


def test_repetition_includes_skill_documents_after_other_sources(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    first = tmp_path / "first"
    skill = tmp_path / "skill"
    first.write_text("x")
    skill.write_text("doc")
    for i, p in enumerate([first, first] + [skill] * 11):
        m.record_page(str(i), m.read_page(p))
    assert m.counters()["repeated_reads"] == 11


def test_large_json_selection_returns_explicit_bounded_not_delivered(tmp_path, monkeypatch):
    from a3_research_agent.harness import file_tools
    from a3_research_agent.harness.tool_contracts import ToolInputError

    raw = json.dumps({"rows": ["large result " * 10000]}).encode()
    with pytest.raises(ToolInputError) as error:
        file_tools.select_fields(raw, ["rows"], "/workspace/report.json")
    assert error.value.code == "field_selection_exceeds_page_budget"
    assert len(error.value.hint) < 1000


def test_identical_page_reference_requires_visible_content(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    path = tmp_path / "data"
    path.write_text("example")
    page = m.read_page(path)
    messages = [m.context_manager.output_message({**page, "receipt_id": "first"}, "first")]
    assert m.visible_page_reference(page, messages)["previous_call_id"] == "first"
    assert m.visible_page_reference(page, []) is None
    path.write_text("updated")
    assert m.visible_page_reference(m.read_page(path), messages) is None
