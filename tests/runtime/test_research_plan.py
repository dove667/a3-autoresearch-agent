import json

from a3_research_agent.harness import agent


def call(name, **arguments):
    return agent.dispatch_tool_call({"name": name, "arguments": json.dumps(arguments)})


def test_plan_tracks_dependencies_evidence_and_job_results(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    job = "a" * 32
    result = call(
        "plan",
        action="update",
        revision=0,
        direction={"objective": "Expand ACT training data"},
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
    assert (
        call("plan", action="update", revision=1, steps=[{"id": "pilot", "status": "done"}])[
            "error"
        ]
        == "plan_validation"
    )
    agent.operational_state.record_gateway("POST", "/jobs", {"job_id": job, "status": "queued"})
    result = call(
        "plan",
        action="update",
        revision=1,
        steps=[
            {"id": "pilot", "status": "active", "jobs": [job]},
        ],
    )
    assert result["ready"] == ["audit"]
    agent.operational_state.record_wait(
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
    for variable in ("A3_WORKSPACE", "A3_INPUT_ROOT", "A3_REFERENCE_ROOT"):
        monkeypatch.setenv(variable, str(tmp_path))
    view = json.loads(agent.operational_message()["content"])
    assert view["runtime_paths"]["inputs"] == str(tmp_path)
    assert "1000 unique eligible" in json.dumps(view)
    assert list((tmp_path / "conversation/context-history").glob("plan-revision-*"))


def test_plan_show_orders_steps_by_dependencies_and_priority(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    call(
        "plan",
        action="update",
        revision=0,
        direction={"objective": "Test dependency order"},
        steps=[
            {
                "id": "later",
                "task": "later",
                "criterion": "later done",
                "depends_on": ["first"],
            },
            {"id": "first", "task": "first", "criterion": "first done", "priority": 100},
            {
                "id": "independent",
                "task": "independent",
                "criterion": "independent done",
                "priority": 50,
            },
        ],
    )
    view = call("plan", action="show")
    assert view["order"] == ["first", "independent", "later"]
    assert list(view["steps"]) == view["order"]
    assert view["steps"]["later"]["depends_on"] == ["first"]


def test_decision_refresh_reconciles_a_historical_job_without_changing_the_plan(
    tmp_path, monkeypatch
):
    for variable in ("A3_WORKSPACE", "A3_INPUT_ROOT", "A3_REFERENCE_ROOT"):
        monkeypatch.setenv(variable, str(tmp_path))
    job = "a" * 32
    call(
        "plan",
        action="update",
        revision=0,
        direction={"objective": "Compare evaluation outcomes"},
        steps=[
            {"id": "evaluate", "task": "Evaluate", "criterion": "Check report", "jobs": [job]},
            {
                "id": "analyze",
                "task": "Analyze",
                "criterion": "Write findings",
                "depends_on": ["evaluate"],
            },
        ],
    )
    before = call("plan", action="show")
    assert before["ready"] == []
    assert "job_status" in before["next_action"]

    requests = []

    def observe(method, path, body, **kwargs):
        assert (method, path, body) == ("POST", "/jobs/observe", {"job_ids": [job]})
        requests.append(job)
        return {
            "jobs": [
                {
                    "job_id": job,
                    "status": "completed",
                    "access": "read_only",
                    "owner_scope": "prior-agent",
                    "owner_run": "prior-run",
                    "observed_at": 100,
                }
            ],
        }

    monkeypatch.setattr(agent, "req", observe)
    source = agent.planning.load()
    refreshed = json.loads(agent.operational_message(observe_jobs=True)["content"])
    agent.operational_message(observe_jobs=True)
    assert requests == [job]
    assert refreshed["job_reconciliation"]["status"] == "observed"
    assert agent.planning.load() == source
    current = call("plan", action="show")
    assert current["review_due"] == [{"step": "evaluate", "route": "", "jobs": {job: "completed"}}]
    assert current["steps"]["evaluate"]["status"] == "pending"
    assert current["steps"]["analyze"]["unmet_dependencies"] == ["evaluate"]
    receipt = current["steps"]["evaluate"]["job_observations"][job]
    assert receipt["owner_run"] == "prior-run" and receipt["access"] == "read_only"
    unrelated_jobs = [
        {"job_id": f"{index:032x}", "status": "completed", "observed_at": 200 + index}
        for index in range(65)
    ]
    agent.operational_state.record_gateway("GET", "/jobs", {"jobs": unrelated_jobs})
    assert len(agent.operational_state.load()["jobs"]) == 65
    assert call("plan", action="show")["steps"]["evaluate"]["observed_jobs"] == {job: "completed"}


def test_list_and_wait_update_lifecycle_without_regressing_to_cached_rows():
    job = "b" * 32
    operations = agent.operational_state
    operations.record_gateway(
        "GET",
        "/jobs?scope=other",
        {"jobs": [{"job_id": job, "status": "running", "observed_at": 100, "access": "read_only"}]},
    )
    operations.record_gateway(
        "POST",
        "/jobs/wait",
        {"jobs": [{"job_id": job, "status": "queued", "observed_at": 90}]},
    )
    operations.record_gateway(
        "POST",
        "/jobs/wait",
        {"jobs": [{"job_id": job, "status": "observation_error", "observed_at": 101}]},
    )
    receipt = operations.load()["jobs"][job]
    assert receipt["status"] == "running"
    assert receipt["observed_at"] == 100
    assert receipt["access"] == "read_only"
    assert receipt["observation_error"] == "job_metadata_unavailable"
    operations.record_gateway(
        "POST",
        "/jobs/wait",
        {"jobs": [{"job_id": job, "status": "completed", "observed_at": 110}]},
    )
    receipt = operations.load()["jobs"][job]
    assert receipt["status"] == "completed" and "observation_error" not in receipt


def test_memory_search_preserves_sources_and_superseded_notes(tmp_path, monkeypatch):
    from a3_research_agent.harness import memory
    from a3_research_agent.harness.memory import client

    monkeypatch.setattr(
        client,
        "call",
        lambda args: memory.dispatch(args, root=tmp_path / "memory", run_id="test-run"),
    )
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
    assert call("memory", **corrected)["id"] == new["id"]
    assert len((tmp_path / "memory/entries.jsonl").read_text().splitlines()) == 2
    assert [
        row["id"] for row in call("memory", action="search", query="DAgger checkpoint")["items"]
    ] == [new["id"]]
    assert "jobs/second/report.json" in call("memory", action="read", id=new["id"])["refs"]
    assert "adapter" in call("memory", action="read", id=old["id"])["text"]


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
    agent.history_tools.append_history_many(rows)
    query = {"keywords": ["checkpoint"], "recent_rounds": 2}
    evidence = call("recall", **query)["items"]
    assert len(evidence) == 1 and "missing decoder" in evidence[0]["snippet"]
    assert evidence[0]["rounds_ago"] == 2
    assert not any(
        "query echo" in row["snippet"] for row in call("recall", **query, mode="raw")["items"]
    )
