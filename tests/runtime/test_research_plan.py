import json

from a3_research_agent.harness import agent
from a3_research_agent.host.runs.continuation import HarnessStart, prepare_fresh_task


def call(name, **arguments):
    return agent.dispatch_tool_call({"name": name, "arguments": json.dumps(arguments)})


def test_plan_tracks_dependencies_evidence_and_job_results(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    job = "a" * 32
    result = call(
        "plan",
        action="update",
        revision=0,
        goal="Expand ACT training data",
        steps=[
            {"id": "pilot", "task": "Verify DAgger", "criterion": "One valid saved trajectory"},
            {
                "id": "collect",
                "task": "Collect 1000",
                "criterion": "1000 unique eligible trajectories",
                "depends_on": ["pilot"],
            },
            {"id": "audit", "task": "Audit expert data", "criterion": "Checked manifest"},
        ],
    )
    assert result["revision"] == 1 and result["ready"] == ["pilot", "audit"]
    assert call("wait", seconds=60)["ready"] == ["pilot", "audit"]
    assert (
        call("plan", action="update", revision=1, steps=[{"id": "pilot", "status": "done"}])[
            "error"
        ]
        == "plan_validation"
    )
    agent.memory_state.record_gateway("POST", "/jobs", {"job_id": job, "status": "queued"})
    result = call(
        "plan",
        action="update",
        revision=1,
        steps=[
            {"id": "pilot", "status": "active", "jobs": [job]},
        ],
    )
    assert result["ready"] == ["audit"]
    agent.memory_state.record_wait(
        {
            "reason": "state_changed",
            "changes": {
                "/jobs/" + job: {"status": "failed"},
            },
        }
    )
    assert call("plan", action="show")["ready"] == ["pilot", "audit"]
    assert call("plan", action="update", revision=1, steps=[])["error"] == "stale_plan_revision"
    assert (
        call(
            "plan",
            action="update",
            revision=2,
            reason="Dependency must be bundled",
            steps=[
                {"id": "pilot", "depends_on": ["collect"]},
            ],
        )["error"]
        == "plan_validation"
    )
    result = call(
        "plan",
        action="update",
        revision=2,
        reason="Replacement pilot verified",
        steps=[
            {"id": "pilot", "status": "done", "evidence": ["outputs/pilot/report.json"]},
        ],
    )
    assert result["ready"] == ["collect", "audit"]
    assert agent.operational_message()["content"].find("1000 unique eligible") >= 0
    assert list((tmp_path / "context-history").glob("plan-revision-*"))


def test_fresh_task_archives_old_plan_and_resume_retains_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    call(
        "plan",
        action="update",
        revision=0,
        goal="old research",
        steps=[
            {"id": "old", "task": "old pending", "criterion": "old evidence"},
        ],
    )
    source = HarnessStart(
        ["python", "agent.py"],
        {
            "checkpoint.json": "[]",
            "research-plan.json": (tmp_path / "research-plan.json").read_text(),
        },
    )
    fresh, metadata = prepare_fresh_task(source, "new task")
    assert call("plan", action="show")["goal"] == "old research"
    assert "research-plan.json" not in fresh.files
    archive = metadata["operational_task_archives"]["research-plan.json"]
    assert fresh.files[archive] == source.files["research-plan.json"]


def test_memory_search_preserves_sources_and_superseded_notes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = call(
        "memory",
        action="write",
        content="DAgger checkpoint loading requires adapter",
        kind="failure",
        refs=["jobs/first/stderr.log"],
    )
    corrected = {
        "action": "write",
        "content": "DAgger checkpoint loading needs decoder beside trainer",
        "kind": "lesson",
        "refs": ["jobs/second/report.json"],
        "scope": "2x10 P142",
        "supersedes": old["id"],
    }
    new = call("memory", **corrected)
    assert call("memory", **corrected) == new
    assert len((tmp_path / "memory.jsonl").read_text().splitlines()) == 2
    assert [row["id"] for row in call("memory", action="search", key="DAgger checkpoint")] == [
        new["id"]
    ]
    assert "jobs/second/report.json" in call("memory", action="read", key=new["id"])
    assert "adapter" in call("memory", action="read", key=old["id"])


def test_recall_defaults_to_evidence_and_can_search_raw_calls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rows = [
        {
            "type": "function_call",
            "name": "gateway",
            "call_id": "1",
            "arguments": '{"path":"checkpoint"}',
        },
        {
            "type": "function_call_output",
            "call_id": "1",
            "output": json.dumps({"stderr": "checkpoint missing decoder"}),
        },
        {
            "type": "function_call",
            "name": "recall",
            "call_id": "2",
            "arguments": '{"keywords":["checkpoint"]}',
        },
        {"type": "function_call_output", "call_id": "2", "output": '["checkpoint query echo"]'},
    ]
    agent.recall_tools.append_history_many(rows)
    query = {"keywords": ["checkpoint"], "n": 2, "m": 1}
    evidence = call("recall", **query)
    assert len(evidence) == 1 and "missing decoder" in evidence[0]
    assert "2轮前" in evidence[0]
    assert any("query echo" in row for row in call("recall", **query, mode="raw"))
