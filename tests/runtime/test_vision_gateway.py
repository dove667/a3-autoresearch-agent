"""Authenticated chronological frame inspection, without a real provider or SSH."""

import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.inputs.feedback import HumanFeedback


def request_body():
    return {
        "question": "Describe the observed process and uncertainties.",
        "frames": [
            {
                "job_id": "a" * 32,
                "path": "diagnostics/frame-10.png",
                "episode_id": "seed1",
                "step": 10,
                "sim_time_s": 0.5,
                "camera": "front",
            },
            {
                "job_id": "a" * 32,
                "path": "diagnostics/frame-20.png",
                "episode_id": "seed1",
                "step": 20,
                "sim_time_s": 1.0,
                "camera": "front",
            },
        ],
    }


def client_for(tmp_path, *, response=None, forbidden=False, context_budget=None, forward_hook=None):
    raw = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(raw, format="PNG")
    reads, models = [], []

    def remote(operation, payload):
        assert operation == "artifact"
        reads.append(payload)
        if forbidden:
            return {"status": "error", "error": "PermissionError"}
        assert payload["experiment_id"] == "owner-a"
        return {
            "encoding": "base64",
            "data": base64.b64encode(raw.getvalue()).decode(),
            **({"area": payload["area"]} if "area" in payload else {}),
        }

    def forward(**body):
        models.append(body)
        if forward_hook is not None:
            forward_hook()
        return response or {
            "status": "completed",
            "model": "actual-visual-worker",
            "reasoning": {"effort": "medium"},
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Two red frames; motion and contact remain uncertain.",
                        }
                    ],
                }
            ],
        }

    feedback = HumanFeedback(tmp_path / "run")
    feedback.register_content(b"PRIVATE_OPERATOR_FEEDBACK_NOT_FOR_VISION", "owner-a")
    app = create_app(
        model_name="fixture/main",
        main_owner="owner-a",
        sessions={"token-a": "owner-a"},
        forward_model=forward,
        remote_call=remote,
        usage_log=tmp_path / "usage.jsonl",
        task_instructions="PRIVATE_MAIN_PHASE",
        human_feedback=feedback,
        role_models={
            "worker": {
                "model_name": "fixture/visual-worker",
                "request_options": {"reasoning": {"effort": "medium"}, "service_tier": "default"},
            }
        },
        context_budget=context_budget,
    )
    return TestClient(app), reads, models


def test_vision_uses_real_image_items_worker_route_and_private_phase_isolation(tmp_path):
    client, reads, models = client_for(tmp_path)
    res = client.post(
        "/vision/inspect", headers={"Authorization": "Bearer token-a"}, json=request_body()
    )
    assert res.status_code == 200, res.text
    result = res.json()
    assert result["analysis"].startswith("Two red frames")
    assert [f["step"] for f in result["frames"]] == [10, 20]
    assert all(f["original_sha256"] and f["normalized_sha256"] for f in result["frames"])
    assert len(reads) == 2 and all(x["experiment_id"] == "owner-a" for x in reads)
    assert all(x["max_bytes"] == 512 * 1024 for x in reads)
    model = models[0]
    assert model["model"] == "fixture/visual-worker" and model["reasoning"]["effort"] == "medium"
    assert model["tools"] == []
    images = [x for m in model["input"] for x in m["content"] if x["type"] == "input_image"]
    assert len(images) == 2 and all(
        x["image_url"].startswith("data:image/jpeg;base64,") for x in images
    )
    assert "PRIVATE_OPERATOR_FEEDBACK" not in json.dumps(model)
    assert "PRIVATE_MAIN_PHASE" not in json.dumps(model)
    assert "image_url" not in res.text and "base64" not in res.text
    log = json.loads((tmp_path / "usage.jsonl").read_text().splitlines()[-1])
    assert log["model_purpose"] == "vision" and log["vision_frame_count"] == 2
    assert log["response_model"] == "actual-visual-worker"


def test_high_level_tools_reach_owned_vision_without_raw_transport(tmp_path, monkeypatch):
    from a3_research_agent.harness import agent

    client, reads, models = client_for(tmp_path)
    monkeypatch.chdir(tmp_path)
    forwarded = []

    def request(method, path, data=None, **kwargs):
        forwarded.append(path)
        if path == "/sessions/self":
            return {"session_id": "owner-a", "is_main": True, "has_been_main": True}
        response = client.request(
            method, path, headers={"Authorization": "Bearer token-a"}, json=data
        )
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(agent, "req", request)
    status = agent.dispatch_tool_call({"name": "vision_status", "arguments": "{}"})
    assert status["ok"] is True and status["available"] is True
    result = agent.dispatch_tool_call(
        {"name": "vision_inspect", "arguments": json.dumps(request_body())}
    )
    assert result["ok"] is True and result["analysis"].startswith("Two red frames")
    assert len(reads) == 2 and len(models) == 1
    assert all(
        frame["area"] == "outputs" and frame["experiment_id"] == "owner-a" for frame in reads
    )
    assert "PRIVATE_OPERATOR_FEEDBACK" not in json.dumps(models)
    for path in ("/control/inputs", "/visionary/status"):
        denied = agent.dispatch_tool_call(
            {"name": "gateway", "arguments": json.dumps({"method": "GET", "path": path})}
        )
        assert denied["ok"] is False and denied["error"] == "unknown_tool"
    assert "/control/inputs" not in forwarded and "/visionary/status" not in forwarded


def test_vision_requires_auth_and_keeps_artifact_scope(tmp_path):
    client, reads, models = client_for(tmp_path, forbidden=True)
    assert client.post("/vision/inspect", json=request_body()).status_code == 401
    assert not reads and not models
    res = client.post(
        "/vision/inspect", headers={"Authorization": "Bearer token-a"}, json=request_body()
    )
    assert res.status_code == 403
    assert not models


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(model="untrusted-provider"),
        lambda b: b["frames"][0].update(source_owner="other"),
        lambda b: b["frames"][0].update(path="../../secret.png"),
        lambda b: b["frames"][1].update(step=5),
    ],
)
def test_vision_invalid_inputs_do_not_read_or_call_model(tmp_path, mutate):
    client, reads, models = client_for(tmp_path)
    body = request_body()
    mutate(body)
    assert (
        client.post(
            "/vision/inspect", headers={"Authorization": "Bearer token-a"}, json=body
        ).status_code
        == 422
    )
    assert not reads and not models


@pytest.mark.parametrize(
    "response",
    [
        {"status": "incomplete", "output": []},
        {"status": "completed", "output": []},
    ],
)
def test_vision_never_labels_missing_or_incomplete_image_response_success(tmp_path, response):
    client, _, _ = client_for(tmp_path, response=response)
    res = client.post(
        "/vision/inspect", headers={"Authorization": "Bearer token-a"}, json=request_body()
    )
    assert res.status_code == 502


def test_vision_image_reserve_is_admitted_before_model_call(tmp_path):
    budget = {
        "main_context_tokens": 100000,
        "worker_context_tokens": 100000,
        "summary_context_tokens": 100000,
        "trigger_tokens": 64000,
        "target_tokens": 32000,
    }
    client, reads, models = client_for(tmp_path, context_budget=budget)
    body = request_body()
    body["frames"] = [
        dict(body["frames"][0], step=index, sim_time_s=index / 20) for index in range(400)
    ]
    res = client.post("/vision/inspect", headers={"Authorization": "Bearer token-a"}, json=body)
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "vision_budget_exceeded"
    assert not reads and not models


def test_vision_provider_failure_is_sanitized_and_releases_slots(tmp_path):
    def fail():
        raise RuntimeError("PRIVATE_PROVIDER_DETAIL")

    client, _, models = client_for(tmp_path, forward_hook=fail)
    for _ in range(3):
        response = client.post(
            "/vision/inspect", headers={"Authorization": "Bearer token-a"}, json=request_body()
        )
        assert response.status_code == 502
        assert "PRIVATE_PROVIDER_DETAIL" not in response.text
    assert len(models) == 3  # One attempt/request, no hidden retry or leaked capacity.


def test_vision_concurrent_limit_does_not_dispatch_a_third_model(tmp_path):
    import concurrent.futures
    import threading
    import time

    release = threading.Event()

    def hold():
        assert release.wait(5)

    client, _, models = client_for(tmp_path, forward_hook=hold)
    headers = {"Authorization": "Bearer token-a"}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        running = [
            pool.submit(client.post, "/vision/inspect", headers=headers, json=request_body())
            for _ in range(2)
        ]
        try:
            deadline = time.monotonic() + 3
            while len(models) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(models) == 2
            third = client.post("/vision/inspect", headers=headers, json=request_body())
            assert third.status_code == 429
            assert len(models) == 2
        finally:
            release.set()
        assert all(f.result(timeout=3).status_code == 200 for f in running)


def test_vision_allows_more_than_six_frames_within_the_worker_budget(tmp_path):
    client, _, models = client_for(tmp_path)
    body = request_body()
    body["frames"] = [
        dict(body["frames"][0], step=index, sim_time_s=index / 20) for index in range(12)
    ]
    result = client.post("/vision/inspect", headers={"Authorization": "Bearer token-a"}, json=body)
    assert result.status_code == 200
    assert len(result.json()["frames"]) == 12
    assert len(models) == 1


def test_vision_returns_complete_analysis_at_model_output_maximum(tmp_path):
    analysis = "Visible observation. " * 1000
    client, _, models = client_for(
        tmp_path,
        response={
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": analysis}]}],
        },
        context_budget={
            "main_context_tokens": 500000,
            "worker_context_tokens": 500000,
            "summary_context_tokens": 500000,
            "worker_max_output_tokens": 1048576,
        },
    )
    result = client.post(
        "/vision/inspect", headers={"Authorization": "Bearer token-a"}, json=request_body()
    )
    assert result.status_code == 200
    assert result.json()["analysis"] == analysis
    assert models[0]["max_output_tokens"] == 1048576


def test_main_direct_view_attaches_original_frames_only_to_next_decision(tmp_path):
    from a3_research_agent.harness import host_tools
    from a3_research_agent.harness.context.manager import output_message

    client, reads, models = client_for(tmp_path)
    body = request_body()
    body["frames"] *= 4
    body["frames"].sort(key=lambda frame: frame["step"])
    result = host_tools.dispatch(
        "vision_view", body, lambda *_: pytest.fail("Selection must not call a provider")
    )
    assert result["status"] == "frames_selected"
    messages = [
        {
            "type": "function_call",
            "name": "vision_view",
            "call_id": "call_view",
            "arguments": json.dumps(body),
        },
        output_message({"ok": True, **result}, "call_view"),
        {"role": "user", "content": "Current operational state"},
    ]
    headers = {"Authorization": "Bearer token-a"}
    tools = [{"type": "function", "name": "read", "parameters": {"type": "object"}}]
    response = client.post(
        "/v1/responses", headers=headers, json={"input": messages, "tools": tools}
    )
    assert response.status_code == 200, response.text
    assert len(reads) == 8
    assert all(read["area"] == "outputs" for read in reads)
    assert len(models) == 1 and models[0]["model"] == "fixture/main"
    assert models[0]["tools"] == tools
    assert "PRIVATE_MAIN_PHASE" in models[0]["instructions"]
    images = [
        part
        for message in models[0]["input"]
        for part in (message["content"] if isinstance(message.get("content"), list) else [])
        if part["type"] == "input_image"
    ]
    assert len(images) == 8
    assert images[0]["image_url"].startswith("data:image/png;base64,")
    original = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(original, format="PNG")
    assert base64.b64decode(images[0]["image_url"].split(",", 1)[1]) == original.getvalue()
    usage = json.loads((tmp_path / "usage.jsonl").read_text().splitlines()[-1])
    assert usage["model_role"] == "main" and usage["vision_frame_count"] == 8
    assert "image_url" not in json.dumps(usage)
    messages.append({"role": "assistant", "content": "I saw the frames."})
    assert (
        client.post("/v1/responses", headers=headers, json={"input": messages}).status_code == 200
    )
    assert len(reads) == 8 and "image_url" not in json.dumps(models[-1])


@pytest.mark.parametrize("forbidden", [True, False])
def test_main_view_unavailable_frames_give_recoverable_notice(tmp_path, forbidden):
    client, _, models = client_for(tmp_path, forbidden=forbidden)
    body = request_body()
    if not forbidden:
        body["frames"][0]["path"] = "../outside.png"
    messages = [
        {
            "type": "function_call",
            "name": "vision_view",
            "call_id": "call_view",
            "arguments": json.dumps(body),
        },
        {
            "type": "function_call_output",
            "call_id": "call_view",
            "output": '{"ok":true,"status":"frames_selected"}',
        },
    ]
    response = client.post(
        "/v1/responses", headers={"Authorization": "Bearer token-a"}, json={"input": messages}
    )
    assert response.status_code == 200, response.text
    assert "could not attach" in json.dumps(models[0])
    assert "image_url" not in json.dumps(models[0])


def test_direct_view_pairs_calls_in_latest_batch_even_with_provider_reasoning(tmp_path):
    from a3_research_agent.harness.context.manager import output_message

    client, reads, models = client_for(tmp_path)
    view = {
        "type": "function_call",
        "name": "vision_view",
        "call_id": "call_view",
        "arguments": json.dumps(request_body()),
    }
    earlier = {**view, "call_id": "call_earlier"}
    selected = {"ok": True, "status": "frames_selected"}
    messages = [
        earlier,
        output_message(selected, "call_earlier"),
        view,
        {"type": "reasoning", "summary": []},
        {"type": "function_call", "name": "read", "call_id": "call_read", "arguments": "{}"},
        output_message(selected, "call_view"),
        output_message({"ok": True}, "call_read"),
    ]
    response = client.post(
        "/v1/responses", headers={"Authorization": "Bearer token-a"}, json={"input": messages}
    )
    assert response.status_code == 200, response.text
    assert len(reads) == 2
    assert "vision_view (call_view)" in json.dumps(models[0])
    assert "vision_view (call_earlier)" not in json.dumps(models[0])


def test_direct_view_provider_media_rejection_retries_plain_decision(tmp_path):
    from a3_research_agent.harness.context.manager import output_message
    from a3_research_agent.host.models.direct import DirectHTTPError

    attempts = 0

    def reject_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DirectHTTPError(400, "input_image unsupported")

    client, reads, models = client_for(tmp_path, forward_hook=reject_once)
    messages = [
        {
            "type": "function_call",
            "name": "vision_view",
            "call_id": "view",
            "arguments": json.dumps(request_body()),
        },
        output_message({"ok": True, "status": "frames_selected"}, "view"),
    ]
    response = client.post(
        "/v1/responses", headers={"Authorization": "Bearer token-a"}, json={"input": messages}
    )
    assert response.status_code == 200, response.text
    assert len(reads) == 2 and len(models) == 2
    assert "image_url" in json.dumps(models[0])
    assert "image_url" not in json.dumps(models[1])
    assert "provider rejected the images" in json.dumps(models[1])
    log = json.loads((tmp_path / "usage.jsonl").read_text().splitlines()[-1])
    assert log["model_role"] == "main" and log["vision_provider_rejected"] is True


def test_direct_view_text_decision_still_fits_when_images_and_notice_do_not(tmp_path, monkeypatch):
    from a3_research_agent.harness.context.manager import output_message
    from a3_research_agent.host.gateway import models as gateway_models

    client, reads, models = client_for(tmp_path)
    monkeypatch.setattr(gateway_models, "model_input_headroom", lambda *_: 100)
    monkeypatch.setattr(
        gateway_models,
        "estimate_request_tokens",
        lambda arguments, _: 110 if "could not attach" in json.dumps(arguments) else 95,
    )
    messages = [
        {
            "type": "function_call",
            "name": "vision_view",
            "call_id": "view",
            "arguments": json.dumps(request_body()),
        },
        output_message({"ok": True, "status": "frames_selected"}, "view"),
    ]
    response = client.post(
        "/v1/responses", headers={"Authorization": "Bearer token-a"}, json={"input": messages}
    )
    assert response.status_code == 200, response.text
    assert not reads and "image_url" not in json.dumps(models[0])
    assert "frames_unavailable" in json.dumps(models[0])
