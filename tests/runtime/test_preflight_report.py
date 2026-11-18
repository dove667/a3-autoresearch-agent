"""A valid preflight report must not be rejected only because it is detailed."""

import base64
import json

from a3_research_agent.harness import job_preparation, job_tools

CPU_ID = "a" * 32
GPU_ID = "b" * 32


def report_text(size):
    """Return a valid passed report of exactly `size` characters."""
    empty = '{"schema": 1, "status": "passed", "pad": ""}'
    assert size >= len(empty)
    return '{"schema": 1, "status": "passed", "pad": "' + "x" * (size - len(empty)) + '"}'


def test_a_6449_character_report_is_valid():
    raw = report_text(6449)
    assert len(raw) == 6449
    assert job_preparation.report_passed({"encoding": "utf-8", "content": raw, "eof": True})
    encoded = base64.b64encode(raw.encode()).decode()
    assert job_preparation.report_passed({"encoding": "base64", "data": encoded})


def test_report_limit_is_16384_and_still_checks_schema_and_status():
    assert job_preparation.report_passed(
        {"encoding": "utf-8", "content": report_text(16384), "eof": True}
    )
    accepted, problem = job_preparation.report_verdict(
        {"encoding": "utf-8", "content": report_text(16385), "eof": True}
    )
    assert not accepted and problem == "too_large"
    assert "larger than" in job_preparation.REPORT_HINTS[problem]

    accepted, problem = job_preparation.report_verdict(
        {
            "encoding": "utf-8",
            "content": json.dumps({"schema": 2, "status": "passed"}),
            "eof": True,
        }
    )
    assert not accepted and problem == "invalid"
    assert "schema=1" in job_preparation.REPORT_HINTS[problem]
    assert not job_preparation.report_passed(
        {"encoding": "utf-8", "content": b'{"schema": 1, "status": "passed"}', "eof": True}
    )


def test_report_hints_separate_missing_from_oversize():
    assert job_preparation.report_verdict({})[1] == "missing"
    assert "No a3-preflight.json" in job_preparation.REPORT_HINTS["missing"]
    assert job_preparation.report_verdict(
        {"encoding": "utf-8", "content": "{", "eof": True}
    )[1] == "malformed"
    assert job_preparation.report_verdict(
        {"encoding": "base64", "data": "not-base64!"}
    )[1] == "malformed"


def test_submitting_a_prepared_job_accepts_the_6449_character_report(tmp_path):
    raw = report_text(6449)
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST":
            return {"job_id": CPU_ID if not body["gpu"] else GPU_ID, "status": "queued"}
        if "artifacts?" in path:
            return {"encoding": "base64", "data": base64.b64encode(raw.encode()).decode()}
        return {"job_id": CPU_ID, "status": "completed", "exit_code": 0}

    prepared = job_tools.dispatch(
        "job_prepare",
        {"command": ["python", "-m", "train"], "gpu": True},
        request,
        workspace=tmp_path,
    )
    assert prepared["preflight_job_id"] == CPU_ID
    result = job_tools.dispatch(
        "job_submit", {"prepared_id": prepared["prepared_id"]}, request, workspace=tmp_path
    )
    assert result["job_id"] == GPU_ID
    assert [body["gpu"] for method, _, body in calls if method == "POST"] == [False, True]


def test_submitting_a_large_report_returns_an_oversize_hint(tmp_path):
    raw = report_text(16385)

    def request(method, path, body=None):
        if method == "POST":
            return {"job_id": CPU_ID if not body["gpu"] else GPU_ID, "status": "queued"}
        if "artifacts?" in path:
            return {"encoding": "utf-8", "content": raw, "eof": True}
        return {"job_id": CPU_ID, "status": "completed", "exit_code": 0}

    prepared = job_tools.dispatch(
        "job_prepare",
        {"command": ["python", "-m", "train"], "gpu": True},
        request,
        workspace=tmp_path,
    )
    result = job_tools.dispatch(
        "job_submit", {"prepared_id": prepared["prepared_id"]}, request, workspace=tmp_path
    )
    assert result["error"] == "preflight_report_missing"
    assert result["report_problem"] == "too_large"
    assert "larger than" in result["hint"] and "schema=1" in result["hint"]
