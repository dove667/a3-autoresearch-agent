"""Scoped notifications do not invoke models or acknowledge events."""

import json

from fastapi.testclient import TestClient

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.inputs.feedback import HumanFeedback
from a3_research_agent.host.runs.state import RunStateStore


def client(tmp_path, feedback, store=None):
    return TestClient(
        create_app(
            model_name="fixture",
            sessions={"main-token": "main", "other-token": "other"},
            main_owner="main",
            forward_model=lambda **kw: (_ for _ in ()).throw(AssertionError("No model calls")),
            remote_call=lambda *a: (_ for _ in ()).throw(AssertionError("No jobs")),
            usage_log=tmp_path / "usage.jsonl",
            human_feedback=feedback,
            state_store=store,
        )
    )


def test_feedback_signal_is_scoped_stable_and_not_consumed(tmp_path):
    f = HumanFeedback(tmp_path / "run")
    api = client(tmp_path, f)
    headers = {"Authorization": "Bearer main-token"}
    first = api.get("/wait/signals", headers=headers)
    assert first.status_code == 200
    f.register_content(b"PRIVATE_FEEDBACK_BODY", "main")
    second = api.get("/wait/signals", headers=headers).json()
    assert second["cursor"] != first.json()["cursor"]
    assert second["pending"] is True
    assert second == api.get("/wait/signals", headers=headers).json()
    assert "PRIVATE_FEEDBACK_BODY" not in json.dumps(second)
    assert "forwarded_at" not in f.records[0]
    other = api.get("/wait/signals", headers={"Authorization": "Bearer other-token"}).json()
    assert other["feedback"]["count"] == 0
    assert api.get("/wait/signals").status_code == 401
    f.forwarded(["feedback-0001"])
    acknowledged = api.get("/wait/signals", headers=headers).json()
    assert acknowledged["pending"] is False
    assert acknowledged["cursor"] == second["cursor"]


def test_completed_analysis_survives_host_restart_and_excludes_foreign_records(tmp_path):
    f = HumanFeedback(tmp_path / "run")
    store = RunStateStore(tmp_path / "run")
    store.update(
        trajectory_analysis={
            "records": [
                {
                    "owner": "main",
                    "analysis_id": "own-report",
                    "status": "completed",
                    "suggestions": [{"private": "BODY_NOT_VISIBLE"}],
                },
                {
                    "owner": "other",
                    "analysis_id": "FOREIGN_REPORT",
                    "status": "completed",
                    "suggestions": [],
                },
            ]
        }
    )
    headers = {"Authorization": "Bearer main-token"}
    before = client(tmp_path, f, store).get("/wait/signals", headers=headers).json()
    after = (
        client(tmp_path, HumanFeedback(tmp_path / "run"), store)
        .get("/wait/signals", headers=headers)
        .json()
    )
    assert before == after
    assert before["analysis"]["pending_ids"] == ["own-report"]
    assert before["pending"] is True
    assert "BODY_NOT_VISIBLE" not in json.dumps(before)
    assert "FOREIGN_REPORT" not in json.dumps(before)


def test_interrupted_analyzer_at_restart_is_pending_without_new_model_call(tmp_path):
    f = HumanFeedback(tmp_path / "run")
    store = RunStateStore(tmp_path / "run")
    store.update(
        trajectory_analysis={
            "records": [
                {
                    "owner": "main",
                    "analysis_id": "interrupted-report",
                    "status": "running",
                    "suggestions": [],
                },
            ]
        }
    )
    result = (
        client(tmp_path, f, store)
        .get("/wait/signals", headers={"Authorization": "Bearer main-token"})
        .json()
    )
    assert result["pending"] is True
    assert result["analysis"]["status"] == "interrupted"


def test_analysis_response_and_timestamps_do_not_self_generate_new_event():
    from a3_research_agent.host.gateway.wait_signals import snapshot

    record = {"owner": "main", "analysis_id": "report", "status": "completed"}
    before = snapshot("main", is_main=True, manifest={}, feedback=[], analysis=[record])
    after = snapshot(
        "main",
        is_main=True,
        manifest={},
        feedback=[],
        analysis=[{**record, "response": {"decision": "defer"}, "wall_seconds": 99}],
    )
    assert before["cursor"] == after["cursor"]
    assert before["pending"] is True
    assert after["pending"] is False
