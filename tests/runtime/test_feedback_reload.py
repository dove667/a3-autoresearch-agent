"""Operator-confirmed feedback reaches the current main at the next request boundary."""

import json
import sys
import threading
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from runtime.local.host_gateway import create_app
from runtime.local.human_feedback import HumanFeedback

OPERATOR = {"Authorization": "Bearer operator-secret"}
MAIN = {"Authorization": "Bearer main-token"}
WORKER = {"Authorization": "Bearer worker-token"}


def setup(tmp_path, forward=None):
    calls = []
    feedback = HumanFeedback(tmp_path / "run")
    app = create_app(
        model_name="fixture",
        sessions={"main-token": "main", "worker-token": "worker"},
        main_owner="main",
        forward_model=forward or (lambda **kw: calls.append(deepcopy(kw)) or {"output": []}),
        remote_call=lambda *args: pytest.fail("No remote operations"),
        usage_log=tmp_path / "usage.jsonl",
        operator_token="operator-secret",
        human_feedback=feedback,
    )
    return TestClient(app), feedback, calls


def preview(api, text="Expert advice"):
    response = api.post("/control/feedback/preview", headers=OPERATOR, json={"text": text})
    assert response.status_code == 200
    return response.json()


def test_only_operator_can_preview_and_confirm_exact_feedback(tmp_path):
    api, feedback, calls = setup(tmp_path)
    for headers in ({}, MAIN, WORKER):
        assert (
            api.post("/control/feedback/preview", headers=headers, json={"text": "hi"}).status_code
            == 403
        )
        assert (
            api.post(
                "/control/feedback", headers=headers, json={"text": "hi", "confirm": "0" * 64}
            ).status_code
            == 403
        )
    proposal = preview(api)
    assert not feedback.directory.exists()
    assert proposal["owner"] == "main" and proposal["warning"]
    assert (
        api.post(
            "/control/feedback",
            headers=OPERATOR,
            json={"text": "changed", "confirm": proposal["digest"]},
        ).status_code
        == 409
    )
    body = {"text": "Expert advice", "confirm": proposal["digest"]}
    registered = api.post("/control/feedback", headers=OPERATOR, json=body)
    assert registered.status_code == 200
    assert registered.json()["new"] is True
    assert api.post("/control/feedback", headers=OPERATOR, json=body).json()["new"] is False
    original = [
        {"type": "function_call", "call_id": "one", "name": "status", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "one", "output": "running"},
    ]
    for headers in (WORKER, MAIN, MAIN):
        assert (
            api.post("/v1/responses", headers=headers, json={"input": original}).status_code == 200
        )
    assert calls[0]["input"] == original
    assert calls[1]["input"][:-1] == original
    assert calls[1]["input"][-1]["content"].endswith("Expert advice")
    assert calls[2]["input"] == calls[1]["input"]
    restored = HumanFeedback(tmp_path / "run")
    assert len(restored.records) == 1 and restored.records[0]["forwarded_at"]
    usage = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert usage[-1]["human_feedback_ids"] == ["feedback-0001"]


@pytest.mark.parametrize("text", ["", "  ", "x" * 65537, "汉" * 22000])
def test_invalid_feedback_has_no_side_effects(tmp_path, text):
    api, feedback, _ = setup(tmp_path)
    assert (
        api.post("/control/feedback/preview", headers=OPERATOR, json={"text": text}).status_code
        == 422
    )
    assert not feedback.directory.exists()


def test_main_change_invalidates_feedback_confirmation(tmp_path, monkeypatch):
    from runtime.local import host_gateway

    captured = []
    original = host_gateway.EvolutionState

    def track_evolution(*args):
        instance = original(*args)
        captured.append(instance)
        return instance

    monkeypatch.setattr(host_gateway, "EvolutionState", track_evolution)
    api, feedback, _ = setup(tmp_path)
    proposal = preview(api)
    captured[0].main_owner = "worker"
    response = api.post(
        "/control/feedback",
        headers=OPERATOR,
        json={"text": "Expert advice", "confirm": proposal["digest"]},
    )
    assert response.status_code == 409
    assert not feedback.records


def test_shutdown_rejects_feedback_registration(tmp_path):
    api, feedback, _ = setup(tmp_path)
    proposal = preview(api)
    api.app.state.begin_shutdown()
    response = api.post(
        "/control/feedback",
        headers=OPERATOR,
        json={"text": "Expert advice", "confirm": proposal["digest"]},
    )
    assert response.status_code == 503
    assert not feedback.records


@pytest.mark.parametrize("operation", ["save", "event"])
def test_failed_registration_does_not_deliver_feedback(tmp_path, monkeypatch, operation):
    api, feedback, calls = setup(tmp_path)
    proposal = preview(api)

    def unavailable(*args, **kwargs):
        raise OSError("controlled storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(feedback, operation, unavailable)
        result = api.post(
            "/control/feedback",
            headers=OPERATOR,
            json={"text": "Expert advice", "confirm": proposal["digest"]},
        )
    assert result.status_code == 503
    assert not feedback.records
    assert not HumanFeedback(tmp_path / "run").records
    original = [{"role": "user", "content": "work"}]
    assert api.post("/v1/responses", headers=MAIN, json={"input": original}).status_code == 200
    assert calls[0]["input"] == original


def test_inflight_call_finishes_before_feedback_reaches_next_call(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def forward(**kwargs):
        calls.append(deepcopy(kwargs))
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return {"output": []}

    api, _, _ = setup(tmp_path, forward)
    original = [{"role": "user", "content": "work"}]
    results = []
    thread = threading.Thread(
        target=lambda: results.append(
            api.post("/v1/responses", headers=MAIN, json={"input": original})
        )
    )
    thread.start()
    assert entered.wait(5)
    try:
        proposal = preview(api)
        assert (
            api.post(
                "/control/feedback",
                headers=OPERATOR,
                json={"text": "Expert advice", "confirm": proposal["digest"]},
            ).status_code
            == 200
        )
    finally:
        release.set()
        thread.join(5)
    assert results[0].status_code == 200
    assert calls[0]["input"] == original
    assert api.post("/v1/responses", headers=MAIN, json={"input": original}).status_code == 200
    assert calls[1]["input"][-1]["content"].endswith("Expert advice")


def test_cli_preview_and_explicit_confirmation(tmp_path, monkeypatch, capsys):
    from runtime.local import reload_feedback
    from runtime.local.run_state import RunStateStore

    api, feedback, _ = setup(tmp_path)
    run = tmp_path / "run"
    RunStateStore(run).save(
        {"endpoint": "http://host.docker.internal:1234", "operator_token": "operator-secret"}
    )
    source = tmp_path / "human-feedback.md"
    source.write_text("Expert advice")

    def connect(**kwargs):
        api.headers.update(kwargs["headers"])
        return api

    monkeypatch.setattr(reload_feedback.httpx, "Client", connect)
    arguments = ["reload_feedback", str(run), "--feedback-file", str(source)]
    monkeypatch.setattr(sys, "argv", arguments)
    reload_feedback.main()
    output = capsys.readouterr().out
    assert "仅预览" in output and "Expert advice" in output and "operator-secret" not in output
    assert not feedback.records
    digest = preview(api)["digest"]
    source.write_text("Updated advice")
    monkeypatch.setattr(sys, "argv", arguments + ["--confirm", digest])
    with pytest.raises(SystemExit, match="过期"):
        reload_feedback.main()
    assert not feedback.records
    source.write_text("Expert advice")
    reload_feedback.main()
    assert feedback.records[0]["text"] == "Expert advice"
