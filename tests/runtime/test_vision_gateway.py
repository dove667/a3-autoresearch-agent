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
        return {"encoding": "base64", "data": base64.b64encode(raw.getvalue()).decode()}

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
        phase_instructions="PRIVATE_MAIN_PHASE",
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
    client, _, models = client_for(tmp_path, context_budget=budget)
    body = request_body()
    third = {**body["frames"][-1], "step": 30, "sim_time_s": 1.5}
    body["frames"].append(third)
    res = client.post("/vision/inspect", headers={"Authorization": "Bearer token-a"}, json=body)
    assert res.status_code == 422
    assert not models


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
