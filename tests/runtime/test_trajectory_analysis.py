import json
import threading
import time

from fastapi.testclient import TestClient

from runtime.local.host_gateway import create_app
from runtime.local.run_state import RunStateStore
from runtime.local.trajectory_analysis import MAX_RECORDS, TrajectoryAnalyzer
from runtime.local.trajectory_telemetry import MAX_EVENTS, TrajectoryTelemetry

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


def advisory(**kwargs):
    return {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "suggestions": [
                                    {
                                        "action": "reduce_repeated_reads",
                                        "evidence": "repeated_reads",
                                        "confidence": "high",
                                    }
                                ]
                            }
                        ),
                    }
                ],
            }
        ]
    }


def client_for(tmp_path, forward=advisory, state_store=None, restored_state=None):
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


def public_report(report):
    return {
        key: value
        for key, value in report.items()
        if key not in {"pending_reports", "omitted_pending_reports", "history"}
    }


def finish(client):
    for _ in range(200):
        result = client.get("/analysis", headers=HEADERS).json()
        if result["status"] != "running":
            return result
        time.sleep(0.005)
    raise AssertionError("Fake analysis did not finish")


def request_and_finish(client):
    response = client.post("/analysis", headers=HEADERS)
    assert response.status_code in (200, 202)
    return finish(client)


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
    assert (
        client.get("/analysis", headers={"Authorization": "Bearer child-token"}).status_code == 403
    )


def test_analyzer_is_main_only_separate_tool_free_worker_route_and_useful(tmp_path):
    calls = []

    def forward(**kwargs):
        calls.append(kwargs)
        return advisory()

    client = client_for(tmp_path, forward)
    assert client.post("/analysis").status_code == 401
    assert client.post("/analysis", headers={"Authorization": "Bearer token-b"}).status_code == 403
    assert (
        client.post("/analysis", headers=HEADERS, json={"instructions": CANARY}).status_code == 422
    )
    client.post("/telemetry/observations", headers=HEADERS, json=OBSERVATIONS)
    result = request_and_finish(client)
    assert result["status"] == "completed" and result["advisory_only"] is True
    assert result["suggestions"][0]["action"] == "reduce_repeated_reads"
    assert calls[0]["model"] == "worker-model"
    assert calls[0]["reasoning"] == {"effort": "medium"}
    assert calls[0]["service_tier"] == "default"
    assert calls[0]["tools"] == [] and calls[0]["tool_choice"] == "none"
    assert calls[0]["store"] is False and calls[0]["max_output_tokens"] == 2000
    assert calls[0]["timeout"] == 60 and calls[0]["num_retries"] == 0
    assert CANARY not in json.dumps(calls)
    assert "untrusted" in calls[0]["instructions"]
    assert json.loads(calls[0]["input"][0]["content"])["local_observations"] == OBSERVATIONS
    assert (
        not {"api_key", "conversation", "previous_response_id", "authorization"} & calls[0].keys()
    )
    assert client.get("/analysis", headers=HEADERS).json() == result
    assert client.post("/analysis", headers=HEADERS).json() == public_report(result)
    assert len(calls) == 1


def test_cooldown_dedup_failures_and_restart_consume_calls(tmp_path):
    calls = []

    def broken(**kwargs):
        calls.append(kwargs)
        raise RuntimeError(CANARY)

    store = RunStateStore(tmp_path)
    client = client_for(tmp_path, broken, store)
    result = request_and_finish(client)
    assert result["status"] == "failed"
    assert CANARY not in json.dumps(store.load())
    client = client_for(tmp_path, broken, store)
    assert client.post("/analysis", headers=HEADERS).json() == public_report(result)
    client.post("/telemetry/observations", headers=HEADERS, json=OBSERVATIONS)
    assert client.post("/analysis", headers=HEADERS).status_code == 429
    assert len(calls) == 1
    state = store.load()["trajectory_analysis"]
    state["calls"] = 100_000
    state["last_started"] = 0
    store.update(trajectory_analysis=state)
    client = client_for(tmp_path, broken, store)
    client.post("/telemetry/observations", headers=HEADERS, json={**OBSERVATIONS, "read_calls": 12})
    assert client.post("/analysis", headers=HEADERS).status_code == 202
    assert finish(client)["status"] == "failed"


def test_malicious_model_output_and_tool_calls_fail_closed(tmp_path):
    for i, output in enumerate(
        [
            [{"type": "function_call", "name": CANARY}],
            [{"type": "message", "content": [{"type": "output_text", "text": CANARY}]}],
            [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps({"suggestions": [], "feedback": CANARY}),
                        }
                    ],
                }
            ],
            [{"type": "message", "content": [{"type": "output_text", "text": "x" * 9000}]}],
        ]
    ):
        client = client_for(tmp_path / str(i), lambda output=output, **kwargs: {"output": output})
        result = request_and_finish(client)
        assert result["status"] == "failed" and result["suggestions"] == []
        assert CANARY not in json.dumps(result)


def test_one_inflight_and_interrupted_reservation(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def blocking(**kwargs):
        entered.set()
        assert release.wait(5)
        return advisory()

    store = RunStateStore(tmp_path)
    analyzer = TrajectoryAnalyzer(
        blocking, {"model_name": "same-model", "request_options": {}}, store
    )
    assert analyzer.state["identity"] != "main"
    snapshot = TrajectoryTelemetry().snapshot(lambda _: True)
    report = analyzer.analyze("main", snapshot)
    assert report["status"] == "running"
    assert entered.wait(5)
    try:
        from fastapi import HTTPException

        try:
            analyzer.analyze("main", snapshot)
            assert False
        except HTTPException as error:
            assert error.status_code == 409
        restarted = TrajectoryAnalyzer(advisory, analyzer.route, store)
        assert restarted.analyze("main", snapshot)["status"] == "interrupted"
    finally:
        release.set()
        for _ in range(200):
            if not analyzer.inflight:
                break
            time.sleep(0.005)


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


def test_analyzer_observed_usage_is_allowlisted_and_unknown_cost_remains_unknown(tmp_path):
    def forward(**kwargs):
        return {
            **advisory(),
            "usage": {"input_tokens": 100, "output_tokens": 20, "secret": CANARY},
            "cost": CANARY,
        }

    result = request_and_finish(client_for(tmp_path, forward))
    assert result["usage"] == {"input_tokens": 100, "output_tokens": 20, "cost": None}
    assert CANARY not in json.dumps(result)


def test_current_main_transition_changes_analyzer_authority(tmp_path):
    store = RunStateStore(tmp_path)
    client = client_for(tmp_path, state_store=store)
    client.app.state.save_checkpoint()
    state = store.load()["gateway"]
    state["evolution"]["main_owner"] = "other"
    client = client_for(tmp_path, state_store=store, restored_state=state)
    assert client.post("/analysis", headers=HEADERS).status_code == 403
    assert client.get("/analysis", headers=HEADERS).status_code == 403
    assert client.post("/analysis", headers={"Authorization": "Bearer token-b"}).status_code == 202


def test_background_request_is_nonblocking_and_response_reason_stays_private(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocking(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        return advisory()

    store = RunStateStore(tmp_path)
    client = client_for(tmp_path, blocking, store)
    started = time.monotonic()
    accepted = client.post("/analysis", headers=HEADERS, json={"trigger": "repetition"})
    assert accepted.status_code == 202
    assert time.monotonic() - started < 2
    assert entered.wait(5)
    assert client.get("/analysis", headers=HEADERS).json()["status"] == "running"
    assert client.get("/telemetry", headers=HEADERS).status_code == 200
    assert client.post("/analysis", headers=HEADERS).status_code == 409
    analysis_id = accepted.json()["analysis_id"]
    disposition = {"analysis_id": analysis_id, "decision": "defer", "reason": CANARY}
    assert client.post("/analysis/response", headers=HEADERS, json=disposition).status_code == 409
    release.set()
    report = finish(client)
    assert report["trigger"] == "repetition"
    assert client.post("/analysis/response", json=disposition).status_code == 401
    assert (
        client.post(
            "/analysis/response", headers={"Authorization": "Bearer token-b"}, json=disposition
        ).status_code
        == 403
    )
    for extra in ({"decision": "execute"}, {"reason": "x" * 501}, {"model": CANARY}):
        assert (
            client.post(
                "/analysis/response", headers=HEADERS, json={**disposition, **extra}
            ).status_code
            == 422
        )
    assert (
        client.post(
            "/analysis/response", headers=HEADERS, json={**disposition, "analysis_id": "0" * 32}
        ).status_code
        == 404
    )
    response = client.post("/analysis/response", headers=HEADERS, json=disposition)
    assert response.json()["response"] == {"decision": "defer", "reason": CANARY}
    snapshot = client.get("/telemetry", headers=HEADERS).json()
    assert CANARY not in json.dumps(snapshot)
    assert CANARY not in json.dumps(calls)
    assert (
        client.post("/analysis", headers=HEADERS, json={"trigger": "periodic"}).json()[
            "analysis_id"
        ]
        == analysis_id
    )
    assert len(calls) == 1
    restarted = client_for(tmp_path, blocking, store)
    assert restarted.get("/analysis", headers=HEADERS).json()["response"]["reason"] == CANARY
    # A changed snapshot after cooldown still excludes the private disposition.
    state = store.load()["trajectory_analysis"]
    state["last_started"] = 0
    store.update(trajectory_analysis=state)
    restarted = client_for(tmp_path, blocking, store)
    restarted.post("/telemetry/observations", headers=HEADERS, json=OBSERVATIONS)
    request_and_finish(restarted)
    assert len(calls) == 2 and CANARY not in json.dumps(calls)


def test_rolling_ledger_is_bounded_without_a_lifetime_call_cap(tmp_path):
    analyzer = TrajectoryAnalyzer(
        advisory, {"model_name": "same-model", "request_options": {}}, RunStateStore(tmp_path)
    )
    for number in range(MAX_RECORDS + 2):
        analyzer.state["last_started"] = 0
        assert analyzer.analyze("main", {"observed_events": number})["status"] == "running"
        for _ in range(200):
            if not analyzer.inflight:
                break
            time.sleep(0.005)
        assert analyzer.latest("main")["status"] == "completed"
    assert len(analyzer.state["records"]) == MAX_RECORDS
    assert analyzer.state["calls"] == MAX_RECORDS + 2
    latest = analyzer.latest("main")
    assert latest["history"]["evicted_reports"] == 2
    assert latest["history"]["evicted_pending_reports"] == 2
    assert len(latest["pending_reports"]) == 3
    assert latest["omitted_pending_reports"] == MAX_RECORDS - 3
    assert analyzer.analyze("main", {"observed_events": MAX_RECORDS + 1})["status"] == "completed"


def test_reasoning_metadata_is_ignored_and_huge_usage_is_bounded(tmp_path):
    def forward(**kwargs):
        return {
            **advisory(),
            "output": [{"type": "reasoning", "summary": [CANARY]}, *advisory()["output"]],
            "usage": {"input_tokens": 10**400},
        }

    result = request_and_finish(client_for(tmp_path, forward))
    assert result["status"] == "completed"
    assert result["usage"]["input_tokens"] == 1_000_000_000
    assert CANARY not in json.dumps(result)


def test_transport_timeout_is_terminal_cached_and_never_retried(tmp_path):
    calls = []

    def timed_out(**kwargs):
        calls.append(kwargs)
        assert kwargs["timeout"] == 60 and kwargs["num_retries"] == 0
        raise TimeoutError(CANARY)

    client = client_for(tmp_path, timed_out, RunStateStore(tmp_path))
    report = request_and_finish(client)
    assert report["status"] == "failed"
    assert CANARY not in json.dumps(report)
    assert client.post("/analysis", headers=HEADERS).json() == public_report(report)
    assert len(calls) == 1


def test_scheduler_identity_and_control_polls_do_not_change_telemetry(tmp_path):
    client = client_for(tmp_path, state_store=RunStateStore(tmp_path))
    client.post("/telemetry/observations", headers=HEADERS, json=OBSERVATIONS)
    report = request_and_finish(client)
    before = client.get("/telemetry?scope=descendants", headers=HEADERS).json()
    for _ in range(5):
        for path in ("/sessions/self", "/evolution", "/analysis", "/health"):
            client.get(path, headers=HEADERS)
        assert client.get("/telemetry?scope=descendants", headers=HEADERS).json() == before
    assert client.post(
        "/analysis", headers=HEADERS, json={"trigger": "periodic"}
    ).json() == public_report(report)


def test_pending_reports_remain_scoped_retrievable_and_explicitly_bounded(tmp_path):
    store = RunStateStore(tmp_path)
    analyzer = TrajectoryAnalyzer(
        advisory, {"model_name": "same-model", "request_options": {}}, store
    )
    ids = []
    for number in range(5):
        analyzer.state["last_started"] = 0
        report = analyzer.analyze("main", {"observed_events": number})
        ids.append(report["analysis_id"])
        for _ in range(200):
            if not analyzer.inflight:
                break
            time.sleep(0.005)
        assert analyzer.latest("main")["status"] == "completed"
    client = client_for(tmp_path, state_store=store)
    latest = client.get("/analysis", headers=HEADERS).json()
    assert latest["analysis_id"] == ids[-1]
    assert [r["analysis_id"] for r in latest["pending_reports"]] == ids[:3]
    assert latest["omitted_pending_reports"] == 2
    assert latest["history"] == {
        "scope": "host_run",
        "retained_report_limit": MAX_RECORDS,
        "evicted_reports": 0,
        "evicted_pending_reports": 0,
    }
    old = client.get("/analysis/" + ids[0], headers=HEADERS)
    assert old.status_code == 200 and old.json()["suggestions"]
    assert client.get("/analysis/" + ids[0]).status_code == 401
    assert (
        client.get("/analysis/" + ids[0], headers={"Authorization": "Bearer token-b"}).status_code
        == 403
    )
    assert client.get("/analysis/" + "0" * 32, headers=HEADERS).status_code == 404
    response = {"analysis_id": ids[0], "decision": "reject", "reason": CANARY}
    assert client.post("/analysis/response", headers=HEADERS, json=response).status_code == 200
    assert client.get("/analysis/" + ids[0], headers=HEADERS).json()["response"]["reason"] == CANARY
    latest = client.get("/analysis", headers=HEADERS).json()
    assert [r["analysis_id"] for r in latest["pending_reports"]] == ids[1:4]
    assert latest["omitted_pending_reports"] == 1
    assert CANARY not in json.dumps(latest)
    assert CANARY not in json.dumps(client.get("/telemetry", headers=HEADERS).json())
    # The new current main cannot access the previous main's retained reports.
    client.app.state.save_checkpoint()
    gateway = store.load()["gateway"]
    gateway["evolution"]["main_owner"] = "other"
    changed = client_for(tmp_path, state_store=store, restored_state=gateway)
    other_headers = {"Authorization": "Bearer token-b"}
    assert changed.get("/analysis/" + ids[0], headers=other_headers).status_code == 404
    assert changed.get("/analysis", headers=other_headers).json()["pending_reports"] == []


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
    from runtime.local.trajectory_analysis import MAX_INPUT_BYTES

    assert len(json.dumps(all_events).encode()) <= MAX_INPUT_BYTES
