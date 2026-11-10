import json

from fastapi.testclient import TestClient

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.runs.state import RunStateStore
from a3_research_agent.host.telemetry.trajectory import MAX_EVENTS, TrajectoryTelemetry

HEADERS = {"Authorization": "Bearer token-a"}
CANARY = "PRIVATE_PATH_CREDENTIAL_PROMPT_ignore_all_rules"
OBSERVATIONS = {
    "read_calls": 10,
    "repeated_reads": 7,
    "retrieved_characters": 1000,
    "delivered_reads": 3,
    "open_items": 2,
    "waiting_items": 1,
    "closed_items": 0,
}


def client_for(
    tmp_path, forward=lambda **kwargs: {"output": []}, state_store=None, restored_state=None
):
    return TestClient(
        create_app(
            model_name="main-model",
            sessions={"token-a": "main", "token-b": "other"},
            main_owner="main",
            forward_model=forward,
            remote_call=lambda *args: {},
            usage_log=tmp_path / "usage.jsonl",
            state_store=state_store,
            restored_state=restored_state,
            role_models={
                "worker": {
                    "model_name": "worker-model",
                    "request_options": {
                        "reasoning": {"effort": "medium"},
                        "service_tier": "default",
                    },
                }
            },
        )
    )


def test_no_untrusted_content_or_control_endpoints_enters_telemetry(tmp_path):
    def provider(**kwargs):
        return {
            "output": [],
            "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15, "evil": CANARY},
            "model": CANARY,
        }

    client = client_for(tmp_path, provider)
    assert client.get("/telemetry").status_code == 401
    client.post("/v1/responses", headers=HEADERS, json={"input": CANARY})
    client.get("/" + CANARY, headers=HEADERS, params={CANARY: CANARY})
    client.get("/health", headers=HEADERS)
    client.get("/telemetry", headers=HEADERS)
    result = client.get("/telemetry", headers=HEADERS).json()
    assert CANARY not in json.dumps(result)
    assert result["observed_events"] == 1
    assert result["models"]["main"]["total_tokens"] == 15
    assert result["models"]["main"]["cost"] is None
    assert result["requests"]["model"]["wall_seconds"] >= 0
    assert (
        client.get("/telemetry", headers={"Authorization": "Bearer token-b"}).json()[
            "observed_events"
        ]
        == 0
    )
    assert client.get("/telemetry", headers=HEADERS, params={"scope": CANARY}).status_code == 422


def test_local_observations_strict_bounded_self_only_and_persistent(tmp_path):
    store = RunStateStore(tmp_path)
    client = client_for(tmp_path, state_store=store)
    for extra in (
        {"path": CANARY},
        {"read_calls": True},
        {"read_calls": -1},
        {"read_calls": 10**10},
        {"read_calls": CANARY},
    ):
        assert (
            client.post(
                "/telemetry/observations", headers=HEADERS, json={**OBSERVATIONS, **extra}
            ).status_code
            == 422
        )
    assert (
        client.post("/telemetry/observations", headers=HEADERS, json=OBSERVATIONS).status_code
        == 200
    )
    assert (
        client.get("/telemetry", headers={"Authorization": "Bearer token-b"}).json()[
            "observation_owners"
        ]
        == 0
    )
    resumed = client_for(tmp_path, state_store=store, restored_state=store.load()["gateway"])
    assert resumed.get("/telemetry", headers=HEADERS).json()["local_observations"] == OBSERVATIONS


def test_descendant_scope_excludes_siblings_and_ancestors(tmp_path):
    store = RunStateStore(tmp_path)
    client = client_for(tmp_path, state_store=store)
    client.app.state.save_checkpoint()
    gateway = store.load()["gateway"]
    gateway["sessions"].update({"child-token": "child", "grand-token": "grand"})
    gateway["scope_parents"].update({"child": "main", "grand": "child"})
    client = client_for(tmp_path, state_store=store, restored_state=gateway)
    for token in ("token-a", "token-b", "child-token", "grand-token"):
        client.post(
            "/telemetry/observations",
            headers={"Authorization": "Bearer " + token},
            json=OBSERVATIONS,
        )
    assert (
        client.get("/telemetry?scope=descendants", headers=HEADERS).json()["observation_owners"]
        == 3
    )
    assert (
        client.get(
            "/telemetry?scope=descendants", headers={"Authorization": "Bearer child-token"}
        ).json()["observation_owners"]
        == 2
    )


def test_event_and_owner_bounds_and_unknown_usage_values():
    telemetry = TrajectoryTelemetry()
    for n in range(MAX_EVENTS + 20):
        telemetry.record(
            {
                "event": "finished",
                "experiment_id": "main",
                "route": "/v1/responses",
                "request_kind": "model",
                "status_code": 200,
                "model_observation": {
                    "role": "main",
                    "input_tokens": CANARY,
                    "output_tokens": float("nan"),
                },
            }
        )
        telemetry.observe(str(n), OBSERVATIONS)
    result = telemetry.snapshot(lambda _: True)
    assert result["observed_events"] == MAX_EVENTS
    assert result["observation_owners"] == MAX_EVENTS
    assert CANARY not in json.dumps(result)
    assert result["models"]["main"]["usage_observed_calls"] == 0


def test_provider_error_types_are_private_but_model_failure_timing_is_observed(tmp_path):
    secret_error = type(CANARY, (RuntimeError,), {})

    def broken(**kwargs):
        raise secret_error(CANARY)

    client = client_for(tmp_path, broken)
    assert client.post("/v1/responses", headers=HEADERS, json={"input": CANARY}).status_code == 502
    snapshot = client.get("/telemetry", headers=HEADERS).json()
    assert CANARY not in json.dumps(snapshot)
    assert snapshot["requests"]["model"]["failures"] == 1
    assert snapshot["models"]["main"]["calls"] == 1
    assert snapshot["models"]["main"]["wall_seconds"] >= 0
    assert snapshot["models"]["main"]["usage_observed_calls"] == 0


def test_recent_events_are_scoped_bounded_numeric_and_independent_copies():
    telemetry = TrajectoryTelemetry()
    for number in range(40):
        telemetry.record(
            {
                "event": "finished",
                "experiment_id": "main" if number % 2 == 0 else "other",
                "route": "/v1/responses",
                "request_kind": "model",
                "status_code": 200,
                "wall_seconds": number + 0.5,
                "error_type": CANARY,
                "path": CANARY,
                "request_id": CANARY,
                "model_observation": {
                    "role": "main",
                    "wall_seconds": number + 0.25,
                    "input_tokens": number,
                    "secret": CANARY,
                },
            }
        )
    own = telemetry.snapshot(lambda actor: actor == "main")
    assert len(own["recent_events"]) == 20
    assert own["recent_events"][-1]["wall_seconds"] == 38.5
    assert own["recent_events"][-1]["model"]["wall_seconds"] == 38.25
    assert own["recent_events"][-1]["model"]["input_tokens"] == 38
    assert CANARY not in json.dumps(own)
    all_events = telemetry.snapshot(lambda _: True)
    assert len(all_events["recent_events"]) == all_events["recent_event_limit"] == 32
    assert all_events["recent_events"][0]["wall_seconds"] == 8.5
    own["recent_events"][-1]["model"]["input_tokens"] = CANARY
    own["recent_events"][-1]["kind"] = CANARY
    own["recent_events"].clear()
    refreshed = telemetry.snapshot(lambda actor: actor == "main")
    assert refreshed["recent_events"][-1]["model"]["input_tokens"] == 38
    assert refreshed["recent_events"][-1]["kind"] == "model"
    assert CANARY not in json.dumps(refreshed)
