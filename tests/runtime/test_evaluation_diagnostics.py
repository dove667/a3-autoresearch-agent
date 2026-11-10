import base64
import io
import json
from uuid import UUID

from fastapi.testclient import TestClient
from PIL import Image

from a3_research_agent.harness.waiting import wait_for_jobs
from a3_research_agent.host.gateway.app import create_app


def test_evaluation_wait_delivers_paired_evidence_and_vision_payload(tmp_path, monkeypatch):
    baseline, candidate = "a" * 32, "b" * 32
    monkeypatch.setattr("a3_research_agent.host.gateway.jobs.uuid.uuid4", lambda: UUID(candidate))
    reports = {
        job: {
            "status": "completed",
            "seed": 123,
            "task": "2x10",
            "profile": "basic",
            "max_steps": 30,
            "native_score": {"score": score, "max_score": 20, "success": False},
        }
        for job, score in [(baseline, 5), (candidate, 0)]
    }

    frame = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(frame, format="PNG")
    vision_images = []

    def forward(**body):
        vision_images.extend(
            item for item in body["input"][0]["content"] if item["type"] == "input_image"
        )
        return {
            "status": "completed",
            "model": "vision-test",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Test"}]}],
        }

    def remote(operation, payload):
        if operation == "submit":
            return {"job_id": candidate, "status": "queued"}
        if operation == "jobs-status":
            return {"jobs": [{"job_id": job, "status": "completed"} for job in payload["job_ids"]]}
        assert operation == "artifact"
        job = payload["job_id"]
        path = payload["path"]
        assert payload["area"] == "outputs"
        if path.endswith(".png"):
            return {
                "encoding": "base64",
                "data": base64.b64encode(frame.getvalue()).decode(),
                "area": "outputs",
            }
        if path.endswith("manifest.json"):
            data = json.dumps(
                {"episodes": [{"id": "example", "report": "/remote/eval/example.json"}]}
            )
        elif path.endswith("trace.jsonl"):
            data = json.dumps(
                {
                    "step": 10,
                    "sim_time": 0.5,
                    "policy_action": [0.1],
                    "frames": {"observation.images.front": {"path": "frames/front.png"}},
                }
            )
        else:
            data = json.dumps(reports[job])
        return {
            "encoding": "base64",
            "data": base64.b64encode(data.encode()).decode(),
            "area": "outputs",
        }

    app = create_app(
        model_name="test",
        sessions={"token": "agent"},
        forward_model=forward,
        remote_call=remote,
        usage_log=tmp_path / "usage.jsonl",
    )
    client = TestClient(app, headers={"Authorization": "Bearer token"})
    comparison = {
        "baseline_job_id": baseline,
        "baseline_manifest": "eval/manifest.json",
        "candidate_manifest": "eval/manifest.json",
    }
    assert (
        client.post("/jobs", json={"command": ["eval"], "evaluation": comparison}).status_code
        == 200
    )

    def req(method, path, body, **kwargs):
        response = client.request(method, path, json=body)
        assert response.status_code == 200
        return response.json()

    notice = wait_for_jobs(req, [candidate])["changes"]["/jobs/" + candidate]["evaluation"]
    assert notice["status"] == "diagnosis_pending"
    assert notice["pairs"][0]["delta"] == -5
    sample = notice["representative_cases"][0]
    assert len(sample["vision_request"]["frames"]) == 2
    assert sample["vision_request"]["frames"][0]["area"] == "outputs"
    assert sample["trace_sources"]["candidate"]["path"].endswith("trace.jsonl")

    assert client.post("/vision/inspect", json=sample["vision_request"]).status_code == 200
    assert len(vision_images) == 2
    reports[baseline]["max_steps"] = 31
    mismatch = client.post(f"/jobs/{candidate}/evaluation", json=comparison).json()
    assert mismatch["pairs"] == []
    assert mismatch["mismatched_conditions"] == [{"seed": 123, "task": "2x10", "profile": "basic"}]
