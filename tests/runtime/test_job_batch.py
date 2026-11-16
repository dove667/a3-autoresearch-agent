"""Several independent jobs can be prepared or submitted inside one decision turn."""

import base64
import json

from a3_research_agent.harness import job_tools

CPU_ID = "a" * 32
GPU_ID = "b" * 32


def request_fixture():
    calls = []
    report = {"schema": 1, "status": "passed"}

    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST":
            return {"job_id": GPU_ID if body["gpu"] else CPU_ID, "status": "queued"}
        if "artifacts?" in path:
            return {
                "encoding": "base64",
                "data": base64.b64encode(json.dumps(report).encode()).decode(),
            }
        return {"job_id": GPU_ID, "status": "completed", "exit_code": 0}

    return calls, request


def scripts(tmp_path):
    (tmp_path / "scripts").mkdir()
    for name in ("audit.py", "eval.py", "train.py"):
        (tmp_path / "scripts" / name).write_text("print('research')\n")


def test_cpu_batch_submits_each_job_and_reports_every_item(tmp_path):
    scripts(tmp_path)
    calls, request = request_fixture()
    result = job_tools.dispatch(
        "job_submit",
        {
            "jobs": [
                {"command": ["python", "audit.py"], "attachments": [{"path": "scripts/audit.py"}]},
                {"command": ["python", "eval.py"], "attachments": [{"path": "scripts/eval.py"}]},
            ]
        },
        request,
        workspace=tmp_path,
    )
    assert result["count"] == 2 and result["failed"] == 0
    assert [call[2]["command"] for call in calls] == [["python", "audit.py"], ["python", "eval.py"]]
    assert all(item["ok"] for item in result["items"])


def test_batch_reports_partial_failure_without_hiding_success(tmp_path):
    scripts(tmp_path)
    calls, request = request_fixture()
    result = job_tools.dispatch(
        "job_submit",
        {
            "jobs": [
                {"command": ["python", "audit.py"]},
                {"command": ["python", "train.py"], "gpu": True},
            ]
        },
        request,
        workspace=tmp_path,
    )
    assert result["failed"] == 1 and result["count"] == 2
    assert result["items"][0]["ok"] is True
    assert result["items"][1]["error"] == "gpu_job_needs_preparation"
    assert len(calls) == 1


def test_batch_where_every_item_fails_is_a_visible_failure(tmp_path):
    _, request = request_fixture()
    result = job_tools.dispatch(
        "job_submit",
        {"jobs": [{"command": []}, {"command": ["  "]}]},
        request,
        workspace=tmp_path,
    )
    assert result["error"] == "batch_failed" and result["failed"] == 2


def test_gpu_batch_prepares_each_request_with_its_own_preflight(tmp_path):
    scripts(tmp_path)
    calls, request = request_fixture()
    result = job_tools.dispatch(
        "job_prepare",
        {
            "jobs": [
                {"command": ["python", "train.py"], "gpu_memory_mb": 6000},
                {"command": ["python", "eval.py"], "gpu_memory_mb": 4000},
            ]
        },
        request,
        workspace=tmp_path,
    )
    assert result["count"] == 2 and result["failed"] == 0
    prepared = [item["prepared_id"] for item in result["items"]]
    assert len(set(prepared)) == 2
    assert all(item["preflight_job_id"] == CPU_ID for item in result["items"])
    assert [call[2]["gpu"] for call in calls] == [False, False]


def test_prepared_ids_submit_several_gpu_jobs_at_once(tmp_path, monkeypatch):
    from a3_research_agent.harness import job_preparation

    seen = []

    def submit(identifier, request, workspace):
        seen.append(identifier)
        return {"job_id": GPU_ID, "status": "queued"}

    monkeypatch.setattr(job_preparation, "submit", submit)
    _, request = request_fixture()
    identifiers = ["prepared-" + "1" * 32, "prepared-" + "2" * 32]
    result = job_tools.dispatch(
        "job_submit", {"prepared_ids": identifiers}, request, workspace=tmp_path
    )
    assert seen == identifiers
    assert result["count"] == 2 and all(item["ok"] for item in result["items"])


def test_invalid_batch_shapes_are_rejected_by_contract(tmp_path):
    _, request = request_fixture()
    bad_ids = job_tools.dispatch(
        "job_submit", {"prepared_ids": ["not-prepared"]}, request, workspace=tmp_path
    )
    assert bad_ids["error"] == "invalid_prepared_ids"
    empty = job_tools.dispatch("job_submit", {"jobs": []}, request, workspace=tmp_path)
    assert empty["error"] == "invalid_jobs"
    mixed = job_tools.dispatch(
        "job_prepare",
        {"jobs": [{"command": ["python", "a.py"]}], "prepared_ids": []},
        request,
        workspace=tmp_path,
    )
    assert mixed["error"]
