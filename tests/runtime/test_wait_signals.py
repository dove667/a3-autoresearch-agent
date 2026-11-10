"""Scoped notifications do not invoke models or acknowledge events."""

import json

from fastapi.testclient import TestClient

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.inputs.feedback import HumanFeedback


def client(tmp_path, feedback):
    return TestClient(
        create_app(
            model_name="fixture",
            sessions={"main-token": "main", "other-token": "other"},
            main_owner="main",
            forward_model=lambda **kw: (_ for _ in ()).throw(AssertionError("No model calls")),
            remote_call=lambda *a: (_ for _ in ()).throw(AssertionError("No jobs")),
            usage_log=tmp_path / "usage.jsonl",
            human_feedback=feedback,
        )
    )


def test_feedback_signal_is_scoped_stable_and_not_consumed(tmp_path):
    f = HumanFeedback(tmp_path / "run")
    api = client(tmp_path, f)
    headers = {"Authorization": "Bearer main-token"}
    first = api.get("/wait/signals", headers=headers)
    assert first.status_code == 200
    assert set(first.json()) == {"schema", "cursor", "is_main", "inputs", "feedback", "pending"}
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
