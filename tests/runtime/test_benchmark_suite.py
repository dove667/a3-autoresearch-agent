"""The benchmark's own contract: twelve tasks, objective checks, no self-report."""

import json

import pytest

from a3_research_agent.benchmark import runner, sandbox
from a3_research_agent.benchmark.tasks import TASKS


def test_every_task_is_unique_and_checkable():
    ids = [task.id for task in TASKS]
    assert len(ids) == len(set(ids)) == 12
    for task in TASKS:
        assert task.prompt.strip() and task.expect
        names = [name for name, _ in task.expect]
        advertised = {
            tool["name"]
            for tool in __import__(
                "a3_research_agent.harness.tool_catalog", fromlist=["TOOLS"]
            ).TOOLS
        }
        assert set(names) <= advertised, task.id
        assert set(task.forbid) <= advertised, task.id


def call(name, arguments):
    return {"name": name, "arguments": json.dumps(arguments)}


def score(task_id, calls, text=""):
    task = next(item for item in TASKS if item.id == task_id)
    return runner.score(task, calls, [], text)


def test_read_task_needs_the_right_path_and_the_number():
    assert score(
        "read_number", [call("read", {"path": "bench/measure.txt"})], "7 来自 2026-11-10-act-p142"
    )[0]
    assert not score("read_number", [call("read", {"path": "bench/task.md"})], "7")[0]
    assert not score("read_number", [call("read", {"path": "bench/measure.txt"})], "不知道")[0]


def test_order_matters_for_list_then_read():
    ordered = [
        call("list", {"path": "experiments"}),
        call("read", {"path": "experiments/x/act-p142/config.json"}),
    ]
    assert score("list_then_read", ordered, "10010 到 10015")[0]
    assert not score("list_then_read", list(reversed(ordered)), "10010 10015")[0]


def test_batch_task_requires_one_call_with_two_jobs():
    assert score(
        "batch_submit",
        [call("job_submit", {"jobs": [{"command": "echo a"}, {"command": "echo b"}]})],
    )[0]
    single = [call("job_submit", {"command": "echo a"}), call("job_submit", {"command": "echo b"})]
    assert not score("batch_submit", single)[0]


def test_gpu_task_rejects_submitting_without_the_prepared_id():
    prepared = [
        call("job_prepare", {"command": "train"}),
        call("job_submit", {"prepared_id": sandbox.PREPARED}),
    ]
    direct = [call("job_prepare", {"command": "train"}), call("job_submit", {"command": "train"})]
    assert score("gpu_preflight", prepared)[0]
    assert not score("gpu_preflight", direct)[0]


def test_cancelled_job_must_not_be_resubmitted():
    assert score("no_duplicate", [call("job_status", {"job_id": sandbox.JOB_CANCELLED})])[0]
    resubmitted = [
        call("job_status", {"job_id": sandbox.JOB_CANCELLED}),
        call("job_submit", {"command": "train"}),
    ]
    assert not score("no_duplicate", resubmitted)[0]


def test_review_requires_a_number_in_every_metric():
    good = [
        call(
            "plan",
            {
                "action": "review",
                "id": "act-p142",
                "decision": "continue",
                "metrics": ["2x10 7/120"],
            },
        )
    ]
    vague = [
        call(
            "plan",
            {"action": "review", "id": "act-p142", "decision": "continue", "metrics": ["更差"]},
        )
    ]
    assert score("plan_review_metrics", good)[0]
    assert not score("plan_review_metrics", vague)[0]


def test_sandbox_refuses_a_gpu_submit_that_skips_the_preflight():
    invalid = []
    direct = sandbox.execute(
        {"name": "job_submit", "arguments": json.dumps({"command": ["train"], "gpu": True})}, invalid
    )
    assert direct["error"] == "gpu_job_needs_preparation"
    assert "job_prepare" in direct["hint"]
    prepared = sandbox.execute(
        {"name": "job_submit", "arguments": json.dumps({"prepared_id": sandbox.PREPARED})}, invalid
    )
    assert prepared["ok"]


def test_sandbox_reports_contract_violations_like_the_harness():
    invalid = []
    assert (
        sandbox.execute({"name": "read", "arguments": "not json"}, invalid)["error"]
        == "invalid_tool_json"
    )
    assert (
        sandbox.execute(
            {"name": "read", "arguments": json.dumps({"path": "a.txt", "x": 1})}, invalid
        )["error"]
        == "invalid_arguments"
    )
    assert len(invalid) == 2


def test_summary_counts_calls_and_rejections():
    results = [
        {"passed": True, "calls": 3, "invalid": 0},
        {"passed": False, "calls": 1, "invalid": 1},
    ]
    summary = runner.summarize(results)
    assert summary["score"] == "1/2" and summary["tool_calls"] == 4
    assert summary["invalid_rate"] == pytest.approx(0.25)


def test_report_writes_both_files(tmp_path):
    results = [
        {
            "id": "read_number",
            "title": "读取文件并报数",
            "passed": True,
            "detail": "ok",
            "names": ["read"],
        }
    ]
    runner.report(results, "sandbox-model", tmp_path)
    assert "能力回归测试" in (tmp_path / "report.md").read_text()
    assert json.loads((tmp_path / "results.json").read_text())["model"] == "sandbox-model"
