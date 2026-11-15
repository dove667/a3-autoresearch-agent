import json

from a3_research_agent.harness import agent, memory, paths
from a3_research_agent.harness import history as recall
from a3_research_agent.harness import operational_state as state
from a3_research_agent.harness import plan as planning


def test_recall_without_a_guessed_window_finds_older_observations(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recall.append_history_many(
        [
            {"type": "function_call", "name": "gateway", "call_id": "old"},
            {
                "type": "function_call_output",
                "call_id": "old",
                "output": '{"stderr":"checkpoint load failed: missing decoder"}',
            },
            {"type": "function_call", "name": "read", "call_id": "new"},
            {"type": "function_call_output", "call_id": "new", "output": "unrelated"},
        ]
    )
    hits = recall.recall_tool({"keywords": ["checkpoint decoder"]})
    assert len(hits) == 1 and "missing decoder" in hits[0] and "2轮前" in hits[0]
    assert "未找到" in recall.recall_tool({"keywords": ["decoder"], "n": 1, "m": 1})[0]


def test_recall_defaults_do_not_retrieve_prior_retrieval_echoes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recall.append_history_many(
        [
            {"type": "function_call", "name": "recall", "call_id": "echo"},
            {"type": "function_call_output", "call_id": "echo", "output": "decoder success"},
        ]
    )
    assert "未找到" in recall.recall_tool({"keywords": ["decoder"]})[0]


def test_empty_note_search_returns_current_source_linked_notes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = memory.dispatch(
        {"action": "write", "content": "obsolete decoder advice", "refs": ["old-report.json"]},
        root=paths.memory_root(),
    )
    new = memory.dispatch(
        {
            "action": "write",
            "content": "decoder version must match",
            "refs": ["verified-report.json"],
            "supersedes": old["id"],
        },
        root=paths.memory_root(),
    )
    matches = memory.dispatch({"action": "search"}, root=paths.memory_root())["items"]
    assert [row["id"] for row in matches] == [new["id"]]
    assert matches[0]["refs"] == ["verified-report.json"]
    assert (
        "obsolete decoder advice"
        in memory.dispatch({"action": "read", "key": old["id"]}, root=paths.memory_root())["text"]
    )


def test_wait_observes_submitted_job_without_requiring_plan_bookkeeping(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    planning.dispatch(
        {
            "action": "update",
            "revision": 0,
            "goal": "Audit data",
            "steps": [{"id": "audit", "task": "Audit data", "criterion": "Saved audit"}],
        }
    )
    job = "a" * 32
    state.record_gateway("POST", "/jobs", {"job_id": job, "status": "queued"})

    def request(method, path, body=None, **options):
        assert method == "POST" and path == "/jobs/wait"
        assert body["job_ids"] == [job]
        return {
            "reason": "terminal",
            "jobs": [{"job_id": job, "status": "completed", "exit_code": 0}],
        }

    monkeypatch.setattr(agent, "req", request)
    result = agent.dispatch_tool_call({"name": "wait", "arguments": json.dumps({"seconds": 60})})
    assert result["reason"] == "state_changed"
    assert result["changes"][f"/jobs/{job}"]["status"] == "completed"
    assert planning.view()["revision"] == 1
