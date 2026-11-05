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
        "state_" + uuid.uuid4().hex, ROOT / "memory/state.py"
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


def test_closed_claim_survives_reload_without_becoming_acceptance(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    m.progress(
        {
            "action": "update",
            "id": "check",
            "status": "closed",
            "note": "rejected",
            "evidence": ["research/decision.json"],
        }
    )
    restored = load(tmp_path, monkeypatch)
    value = restored.view()["items"]["check"]
    assert value["status"] == "closed" and "not validated" in value["authority"]
    assert not (tmp_path / "run-outcome.json").exists()
    assert list((tmp_path / "context-history").glob("progress-item*")) == []
    restored.progress({"action": "update", "id": "check", "status": "waiting", "evidence": []})
    assert list((tmp_path / "context-history").glob("progress-item*"))


def test_corrupt_state_fails_closed_and_snapshot_remains(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    m.PATH.write_text("broken")
    with pytest.raises(ValueError):
        m.view()
    assert m.PATH.read_text() == "broken"


@pytest.mark.parametrize("args", [{"offset": -1}, {"limit": 0}, {"limit": True}, {"limit": 9000}])
def test_invalid_pages_fail_explicitly(tmp_path, monkeypatch, args):
    m = load(tmp_path, monkeypatch)
    p = tmp_path / "x"
    p.write_text("data")
    with pytest.raises(ValueError):
        m.read_page(p, **args)


def test_repeated_cycles_keep_exact_coverage_and_closed_claim(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    p = tmp_path / "report"
    p.write_text("内容" * 20000)
    m.progress(
        {"action": "update", "id": "report", "status": "closed", "evidence": ["decision.json"]}
    )
    for i in range(30):
        page = m.read_page(p, i * 1000, 1000)
        m.record_page(str(i), page)
        m.mark_delivered(
            [{"type": "function_call_output", "call_id": str(i), "output": json.dumps(page)}]
        )
        m = load(tmp_path, monkeypatch)
    assert m.view()["reads"][0]["delivered"] == [[0, 30000]]
    assert m.view()["items"]["report"]["status"] == "closed"
    assert len(json.dumps(m.view(), ensure_ascii=False).encode()) <= 8000


@pytest.mark.parametrize(
    "reference", ["", ".", "./", "../outside", "https://host/path", "bad\0path"]
)
def test_invalid_evidence_reference_cannot_close_item(tmp_path, monkeypatch, reference):
    m = load(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        m.progress({"action": "update", "id": "x", "status": "closed", "evidence": [reference]})


def test_changed_evidence_is_flagged_without_automatic_reopening(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    p = tmp_path / "decision.json"
    p.write_text("old")
    m.progress({"action": "update", "id": "x", "status": "closed", "evidence": ["decision.json"]})
    p.write_text("new")
    item = m.view()["items"]["x"]
    assert item["evidence_changed_or_unavailable"]
    assert item["status"] == "closed"


def test_new_task_archives_operational_state_but_keeps_source_immutable(tmp_path):
    from a3_research_agent.host.runs.continuation import HarnessStart, prepare_fresh_task

    source = HarnessStart(
        ["python", "agent.py", "--resume"],
        {
            "checkpoint.json": "[]",
            "memory.jsonl": "retained lessons",
            "operational-state.json": json.dumps(
                {"version": 1, "items": {"old": {"status": "closed"}}}
            ),
            "analysis-client/schedule.handoff.json": json.dumps({"id": "a" * 32}),
            "analysis-client/schedule.json": json.dumps(
                {"version": 1, "last_attempt": 123, "latest": {"analysis_id": "old"}}
            ),
        },
        source_version="version-old",
    )
    before = dict(source.files)
    fresh, context = prepare_fresh_task(source, "new task")
    assert source.files == before and fresh.files["memory.jsonl"] == "retained lessons"
    assert (
        context["source_checkpoint_sha256"]
        == __import__("hashlib").sha256(before["checkpoint.json"].encode()).hexdigest()
    )
    assert json.loads(fresh.files["operational-state.json"])["items"] == {}
    assert json.loads(fresh.files["analysis-client/schedule.json"])["last_attempt"] is None
    assert json.loads(fresh.files["analysis-client/schedule.handoff.json"])["id"] is None
    for name, archive in context["operational_task_archives"].items():
        assert fresh.files[archive] == before[name]


def test_sparse_coverage_is_bounded_and_never_fills_gaps(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    p = tmp_path / "sparse"
    p.write_text("a" * 1000)
    for i in range(140):
        m.record_page(str(i), m.read_page(p, i * 2, 1))
    entry = next(iter(m.load()["reads"].values()))
    assert len(entry["retrieved"]) <= 64 and entry["coverage_incomplete"]
    assert all(b - a == 1 for a, b in entry["retrieved"])


def test_identical_operational_claim_is_not_new_progress(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    item = {"action": "update", "id": "x", "status": "waiting", "evidence": []}
    m.progress(item)
    before = m.signals()["progress_updates"]
    m.progress(item)
    assert m.signals()["progress_updates"] == before


def test_identical_decision_write_is_not_new_progress(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    p = tmp_path / "decision.json"
    p.write_text("same")
    m.record_write(p)
    before = m.signals()["progress_updates"]
    m.record_write(p)
    assert m.signals()["progress_updates"] == before
    p.write_text("changed")
    m.record_write(p)
    assert m.signals()["progress_updates"] == before + 1


def test_fresh_job_wait_is_explicit_and_expires(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    clock = [1000]
    monkeypatch.setattr(m.time, "time", lambda: clock[0])
    job = "a" * 32
    m.record_gateway("POST", "/jobs", {"job_id": job, "status": "running"})
    assert m.signals()["waiting"]
    clock[0] += 601
    assert not m.signals()["waiting"]
    m.record_gateway("GET", "/jobs/" + job, {"job_id": job, "status": "completed"})
    assert m.signals()["progress_updates"] == 1
    m.record_gateway("GET", "/jobs/" + job, {"job_id": job, "status": "completed"})
    assert m.signals()["progress_updates"] == 1


@pytest.mark.parametrize("diagnostic_fails", [False, True])
def test_receipt_failure_cannot_hide_successful_job_result(tmp_path, monkeypatch, diagnostic_fails):
    import ast
    from types import SimpleNamespace

    tree = ast.parse((ROOT / "agent.py").read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "tool"]
    result = {"job_id": "a" * 32, "status": "created"}

    def fail(*args):
        raise OSError("receipt failed")

    from a3_research_agent.harness.orchestration import assignments as work_assignments

    def request(method, path, data=None):
        if path == "/sessions/self":
            return {"session_id": "worker"}
        if path == "/evolution":
            return {"http_error": 403}
        return result

    namespace = {
        "work_assignments": work_assignments,
        "web_tools": SimpleNamespace(ROUTES={}),
        "skill_tools": SimpleNamespace(ROUTES={}),
        "memory_state": SimpleNamespace(record_gateway=fail),
        "req": request,
        "log": fail if diagnostic_fails else lambda value: None,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "tool-fixture", "exec"), namespace)  # noqa: S102 - selected trusted local function only
    assert namespace["tool"]("gateway", {"method": "POST", "path": "/jobs", "body": {}}) == result


@pytest.mark.parametrize("diagnostic_fails", [False, True])
def test_receipt_failure_cannot_hide_successful_file_write(tmp_path, diagnostic_fails):
    import ast
    from types import SimpleNamespace

    tree = ast.parse((ROOT / "agent.py").read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "tool"]
    p = tmp_path / "written"

    def fail(*args):
        raise OSError("receipt failed")

    namespace = {
        "web_tools": SimpleNamespace(ROUTES={}),
        "skill_tools": SimpleNamespace(ROUTES={}),
        "memory_state": SimpleNamespace(record_write=fail),
        "pathlib": SimpleNamespace(Path=lambda name: p),
        "log": fail if diagnostic_fails else lambda value: None,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "tool-fixture", "exec"), namespace)  # noqa: S102 - selected trusted local function only
    assert (
        namespace["tool"]("write", {"path": "/workspace/written", "content": "actual result"})
        == "written"
    )
    assert p.read_text() == "actual result"


def test_repetition_includes_skill_documents_after_other_sources(tmp_path, monkeypatch):
    m = load(tmp_path, monkeypatch)
    first = tmp_path / "first"
    skill = tmp_path / "skill"
    first.write_text("x")
    skill.write_text("doc")
    for i, p in enumerate([first, first] + [skill] * 11):
        m.record_page(str(i), m.read_page(p))
    assert m.signals()["repeated_static_reads"] == 11


def test_large_json_selection_returns_explicit_bounded_not_delivered(tmp_path, monkeypatch):
    import ast
    import hashlib

    m = load(tmp_path, monkeypatch)
    tree = ast.parse((ROOT / "agent.py").read_text())
    nodes = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "select_json_fields"
    ]
    ns = {"json": json, "hashlib": hashlib, "context_manager": m.context_manager}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "selected-local-function", "exec"), ns)  # noqa: S102 - trusted local helper only
    raw = json.dumps({"rows": ["large result " * 10000]}).encode()
    result = ns["select_json_fields"](raw, ["rows"], "/workspace/report.json")
    assert result["error"] == "field_selection_exceeds_page_budget" and "fields" not in result
    assert result["sha256"] == hashlib.sha256(raw).hexdigest() and len(json.dumps(result)) < 1000


def test_fresh_task_archives_fork_sidecars_and_old_history_without_loading_them(tmp_path):
    from a3_research_agent.host.runs.continuation import HarnessStart, prepare_fresh_task

    old = {
        "checkpoint.json": '[{"role":"user","content":"old goal"}]',
        "compaction-result.json": '{"checkpoint":[{"role":"user","content":"old summary"}]}',
        "compaction-epoch.json": '{"version":1,"id":"old-task"}',
        "history.jsonl": '{"role":"user","content":"old raw goal"}\n',
        "context-history/compaction-progress.json": '{"old":"spent-call-journal"}',
        "context-history/summary-request-" + "a" * 64 + ".json": '{"status":"dispatched"}',
        "context-failure.json": '{"error":"old failure"}',
        "memory.jsonl": '{"lesson":"retain long-term experience"}\n',
    }
    names = list(old) + ["resume.json", "continuation.json"]
    for name in ["resume.json", "continuation.json"]:
        old[name] = json.dumps({"command": ["python", "agent.py", "--resume"], "files": names})
    start = HarnessStart(["python", "agent.py", "--resume"], dict(old), source_version="prior")
    fresh, context = prepare_fresh_task(start, "new goal")
    assert start.files == old
    assert json.loads(fresh.files["checkpoint.json"])[0]["content"] == "new goal"
    assert json.loads(fresh.files["compaction-result.json"]) is None
    assert json.loads(fresh.files["compaction-epoch.json"])["id"] is None
    assert fresh.files["history.jsonl"] == ""
    assert "context-history/compaction-progress.json" not in fresh.files
    assert "context-history/summary-request-" + "a" * 64 + ".json" not in fresh.files
    assert "context-failure.json" not in fresh.files
    assert fresh.files["memory.jsonl"] == old["memory.jsonl"]
    for name in ["resume.json", "continuation.json"]:
        declared = json.loads(fresh.files[name])["files"]
        assert all(path in fresh.files for path in declared)
    for name, archive in context["operational_task_archives"].items():
        assert fresh.files[archive] == old[name]
