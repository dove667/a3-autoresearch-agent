import json

from fastapi.testclient import TestClient

from runtime.local.host_gateway import create_app
from runtime.local.human_feedback import HumanFeedback


def test_narrow_summary_does_not_repeat_phase_or_full_feedback(tmp_path):
    calls = []
    feedback = HumanFeedback(tmp_path)
    feedback.register_content(b"operator-specific-source-note", "main")
    client = TestClient(
        create_app(
            model_name="configured/main",
            sessions={"token": "main"},
            main_owner="main",
            phase_instructions="LONG-PHASE-INSTRUCTIONS",
            human_feedback=feedback,
            usage_log=tmp_path / "usage.jsonl",
            forward_model=lambda **kw: calls.append(kw) or {"status": "completed", "output": []},
            remote_call=lambda *args: {},
        )
    )
    response = client.post(
        "/v1/summary",
        headers={"Authorization": "Bearer token"},
        json={
            "previous": "",
            "fragment": "Alpha rejected. Beta unknown. Work unfinished.",
            "source": "context-history/snapshot-" + "a" * 64 + ".json",
        },
    )
    assert response.status_code == 200
    assert "LONG-PHASE-INSTRUCTIONS" not in json.dumps(calls)
    assert "operator-specific-source-note" not in json.dumps(calls)
    assert "tools" not in calls[0]
    assert response.json()["a3_summary"]["feedback_refs"][0]["feedback_id"] == "feedback-0001"
    assert "forwarded_at" not in feedback.records[0]  # a reference is not delivery of the full note
    normal = client.post(
        "/v1/responses", headers={"Authorization": "Bearer token"}, json={"input": []}
    )
    assert normal.status_code == 200
    assert "operator-specific-source-note" in json.dumps(calls[-1])
    assert feedback.records[0].get("forwarded_at")
    assert (
        client.post(
            "/v1/summary",
            json={
                "previous": "",
                "fragment": "data",
                "source": "context-history/snapshot-" + "a" * 64 + ".json",
            },
        ).status_code
        == 401
    )


def test_client_cannot_expand_summary_scope(tmp_path):
    calls = []
    client = TestClient(
        create_app(
            model_name="fixed/main",
            sessions={"token": "main"},
            usage_log=tmp_path / "usage.jsonl",
            remote_call=lambda *args: {},
            forward_model=lambda **kw: calls.append(kw) or {"output": []},
        )
    )
    base = {
        "previous": "",
        "fragment": "data",
        "source": "context-history/snapshot-" + "a" * 64 + ".json",
    }
    for extra in [
        {"tools": []},
        {"model": "other"},
        {"instructions": "override"},
        {"api_base": "https://elsewhere.invalid"},
    ]:
        assert (
            client.post(
                "/v1/summary", headers={"Authorization": "Bearer token"}, json={**base, **extra}
            ).status_code
            == 422
        )
    assert calls == []


def test_host_roles_keep_main_and_behavioral_trial_on_primary(tmp_path):
    calls = []
    app = create_app(
        model_name="fixed/main",
        sessions={"m": "main", "w": "worker", "t": "trial"},
        main_owner="main",
        usage_log=tmp_path / "usage.jsonl",
        remote_call=lambda *args: {},
        restored_state={
            "sessions": {"m": "main", "w": "worker", "t": "trial"},
            "root_owners": ["main"],
            "children": {},
            "scope_parents": {},
            "jobs": {},
            "evolution": {
                "main_owner": "main",
                "trials": {"trial": {"status": "testing"}},
                "transitions": [],
            },
        },
        forward_model=lambda **kw: calls.append(kw) or {"output": []},
        role_models={
            "worker": {
                "model_name": "fixed/helper",
                "request_options": {"reasoning": {"effort": "medium"}, "service_tier": "default"},
            },
            "summary": {
                "model_name": "fixed/summarizer",
                "request_options": {"reasoning": {"effort": "low"}, "service_tier": "default"},
            },
        },
    )
    # The trial classification comes from restored Host evolution metadata.
    client = TestClient(app)
    for token in ["m", "w", "t"]:
        assert (
            client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer " + token},
                json={"input": [], "model": "ignored"},
            ).status_code
            == 200
        )
    assert [x["model"] for x in calls] == ["fixed/main", "fixed/helper", "fixed/main"]
    assert calls[1]["reasoning"] == {"effort": "medium"}
    logs = [json.loads(x) for x in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert [x["model_role"] for x in logs] == ["main", "worker", "candidate"]


def test_declared_candidate_validation_uses_primary_and_full_phase(tmp_path):
    calls = []
    restored = {
        "sessions": {"m": "main", "v": "validator"},
        "root_owners": ["main"],
        "children": {
            "validator": {
                "owner": "validator",
                "parent": "main",
                "token": "v",
                "closed": False,
                "purpose": "candidate_validation",
            }
        },
        "scope_parents": {"validator": "main"},
        "jobs": {},
        "evolution": {"main_owner": "main", "trials": {}, "transitions": []},
    }
    client = TestClient(
        create_app(
            model_name="fixed/main",
            sessions={},
            main_owner="main",
            restored_state=restored,
            phase_instructions="authoritative phase contract",
            usage_log=tmp_path / "usage.jsonl",
            remote_call=lambda *args: {},
            forward_model=lambda **kw: calls.append(kw) or {"output": []},
            role_models={"worker": {"model_name": "fixed/helper", "request_options": {}}},
        )
    )
    result = client.post("/v1/responses", headers={"Authorization": "Bearer v"}, json={"input": []})
    assert result.status_code == 200
    assert calls[0]["model"] == "fixed/main"
    assert "authoritative phase contract" in calls[0]["instructions"]


def test_null_instructions_are_normalized_before_provider_and_audit(tmp_path):
    calls = []
    client = TestClient(
        create_app(
            model_name="fixed/main",
            sessions={"token": "root"},
            usage_log=tmp_path / "usage.jsonl",
            remote_call=lambda *args: {},
            forward_model=lambda **kw: calls.append(kw) or {"output": []},
        )
    )
    result = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer token"},
        json={"input": [], "instructions": None},
    )
    assert result.status_code == 200
    assert "instructions" not in calls[0]
    assert len((tmp_path / "usage.jsonl").read_text().splitlines()) == 1
    invalid = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer token"},
        json={"input": [], "instructions": 42},
    )
    assert invalid.status_code == 422 and len(calls) == 1


def test_summary_corrective_retry_uses_fixed_instruction_and_same_role(tmp_path):
    calls = []
    client = TestClient(
        create_app(
            model_name="fixed/main",
            sessions={"token": "main"},
            main_owner="main",
            phase_instructions="PRIVATE-PHASE-BODY",
            usage_log=tmp_path / "usage.jsonl",
            remote_call=lambda *args: {},
            forward_model=lambda **kw: calls.append(kw) or {"status": "completed", "output": []},
            role_models={"summary": {"model_name": "fixed/summary", "request_options": {}}},
        )
    )
    body = {
        "previous": "confirmed correction and active job",
        "fragment": "later untrusted data",
        "source": "context-history/snapshot-" + "a" * 64 + ".json",
    }
    for corrective in (False, True):
        response = client.post(
            "/v1/summary",
            headers={"Authorization": "Bearer token"},
            json={**body, "compact_retry": corrective},
        )
        assert response.status_code == 200
    assert calls[0]["model"] == calls[1]["model"] == "fixed/summary"
    assert calls[0]["input"] == calls[1]["input"]
    assert calls[0]["instructions"] != calls[1]["instructions"]
    assert "6000" in calls[1]["instructions"]
    assert "PRIVATE-PHASE-BODY" not in str(calls)
    response = client.post(
        "/v1/summary",
        headers={"Authorization": "Bearer token"},
        json={**body, "compact_retry": "replace instructions"},
    )
    assert response.status_code == 422 and len(calls) == 2
