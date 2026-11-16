"""The benchmark's own contract: twelve tasks, objective checks, no self-report."""

import json

import pytest

from a3_research_agent.benchmark import runner, sandbox
from a3_research_agent.benchmark.tasks import TASKS


def test_every_task_is_unique_and_checkable():
    ids = [task.id for task in TASKS]
    assert len(ids) == len(set(ids)) == 12
    for task in TASKS:
        assert task.prompt.strip()
        names = [entry[0] for entry in task.expect if not callable(entry)]
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


def test_a_failed_job_needs_both_the_status_and_the_log():
    calls = [
        call("job_status", {"job_id": sandbox.JOB_FAILED}),
        call("read", {"path": "logs/train-f.log"}),
    ]
    assert score("triage_failed_job", calls, f"{sandbox.JOB_FAILED} 是 CUDA out of memory")[0]
    assert not score("triage_failed_job", calls, f"{sandbox.JOB_FAILED} 挂了")[0]
    only_status = [call("job_status", {"job_id": sandbox.JOB_FAILED})]
    assert not score("triage_failed_job", only_status, "out of memory")[0]


def test_read_task_needs_the_right_path_and_the_number():
    assert score(
        "read_eval_number",
        [call("read", {"path": "bench/measure.txt"})],
        "7，来自 2026-11-10-act-p142",
    )[0]
    assert not score("read_eval_number", [call("read", {"path": "bench/task.md"})], "7")[0]
    assert not score("read_eval_number", [call("read", {"path": "bench/measure.txt"})], "不知道")[0]


def test_a_lookup_counts_when_its_fact_is_stated():
    # Looking at the seeds is scored by the stated range, not by the route taken.
    looked = [call("read", {"path": "experiments/x/act-p142/config.json"})]
    assert score("find_eval_seeds", looked, "10010 到 10015")[0]
    assert not score("find_eval_seeds", looked, "不知道")[0]


def test_both_seeds_must_be_submitted():
    batched = [
        call("job_submit", {"jobs": [{"command": ["echo", "a"]}, {"command": ["echo", "b"]}]}),
    ]
    separate = [
        call("job_submit", {"command": ["echo", "a"]}),
        call("job_submit", {"command": ["echo", "b"]}),
    ]
    assert score("batch_two_trainings", batched)[0]
    assert score("batch_two_trainings", separate)[0]
    assert not score("batch_two_trainings", separate[:1])[0]


def test_a_smoke_test_must_not_go_through_gpu_preparation():
    smoke = [call("job_submit", {"command": ["echo", "smoke"]}), call("job_wait", {})]
    assert score("cpu_smoke_train", smoke, "退出码 0")[0]
    prepared = [
        call("job_prepare", {"command": ["echo", "smoke"]}),
        call("job_submit", {"prepared_id": sandbox.PREPARED}),
        call("job_wait", {}),
    ]
    assert not score("cpu_smoke_train", prepared, "0")[0]


def test_gpu_task_rejects_submitting_without_the_prepared_id():
    prepared = [
        call("job_prepare", {"command": ["train"]}),
        call("job_submit", {"prepared_id": sandbox.PREPARED}),
    ]
    direct = [
        call("job_prepare", {"command": ["train"]}),
        call("job_submit", {"command": ["train"], "gpu": True}),
    ]
    assert score("gpu_prepare_then_submit", prepared)[0]
    assert not score("gpu_prepare_then_submit", direct)[0]


def test_review_needs_the_measured_result_not_just_a_review():
    reviewed = [
        call("read", {"path": "experiments/2026-11-10-act-p142/results/report.json"}),
        call(
            "plan",
            {
                "action": "review",
                "id": "act-p142",
                "decision": "continue",
                "metrics": ["2x5 cookies_in_target=29/60"],
            },
        ),
    ]
    assert score("review_completed_run", reviewed[1:], "2×5 入盒 29/60")[0]
    vague = [reviewed[0], call("plan", {"action": "review", "id": "act-p142", "decision": "continue"})]
    assert not score("review_completed_run", vague, "29/60")[0]


def test_a_cancelled_or_running_job_must_not_be_resubmitted():
    assert score("recover_uncertain_submit", [call("job_list", {})], sandbox.JOB_TRAIN)[0]
    resubmitted = [call("job_list", {}), call("job_submit", {"command": ["train"]})]
    assert not score("recover_uncertain_submit", resubmitted, sandbox.JOB_TRAIN)[0]


def test_a_missing_permission_is_reported_not_retried():
    reported = [
        call("research_status", {"status": "blocked", "reason": "需要操作者开通 8 卡权限"}),
    ]
    assert score("blocked_on_operator", reported, "已标记 blocked，等待操作者")[0]
    retried = [
        call("research_status", {"status": "blocked", "reason": "缺权限"}),
        call("job_submit", {"command": ["train"], "gpu": True}),
    ]
    assert not score("blocked_on_operator", retried, "blocked")[0]


def test_a_new_experiment_is_recorded_on_its_route():
    on_route = [call("plan", {"action": "update", "steps": [{"id": "s6", "route": "act-p142"}]})]
    assert score("add_next_experiment", on_route)[0]
    elsewhere = [call("plan", {"action": "update", "steps": [{"id": "s6", "route": "act-baseline"}]})]
    assert not score("add_next_experiment", elsewhere)[0]


def test_sandbox_prepares_a_gpu_submit_by_itself():
    invalid = []
    direct = sandbox.execute(
        {"name": "job_submit", "arguments": json.dumps({"command": ["train"], "gpu": True})}, invalid
    )
    assert direct["status"] == "prepared_pending_preflight"
    assert direct["prepared_id"] == sandbox.PREPARED
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
