"""The runner runs tasks side by side and never loses a finished one."""

import json

from a3_research_agent.benchmark import runner, sandbox
from a3_research_agent.benchmark.tasks import TASKS
from a3_research_agent.harness import drift


def fake_ask(url, key, model, messages, tools, turns, answers=True):
    return (
        [
            {"name": "job_status", "arguments": json.dumps({"job_id": sandbox.JOB_A})},
        ],
        [],
        "0",
        drift.DriftGuard(),
    )


def test_all_tasks_run_in_parallel_and_are_reported(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("A3_UPSTREAM_RESPONSES_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("A3_UPSTREAM_RESPONSES_API_KEY", "k")
    monkeypatch.setenv("A3_UPSTREAM_RESPONSES_MODEL", "m")
    monkeypatch.setattr(runner, "ask", fake_ask)
    summary = runner.run(output=tmp_path, jobs=4)
    assert summary["total"] == len(TASKS)
    rows = json.loads((tmp_path / "results.json").read_text())["tasks"]
    assert [row["id"] for row in rows] == [task.id for task in TASKS]


def test_a_finished_task_is_not_run_again(monkeypatch, tmp_path):
    monkeypatch.setenv("A3_UPSTREAM_RESPONSES_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("A3_UPSTREAM_RESPONSES_API_KEY", "k")
    monkeypatch.setenv("A3_UPSTREAM_RESPONSES_MODEL", "m")
    calls = []

    def counting(*args, **kwargs):
        calls.append(args[4])
        return fake_ask(*args, **kwargs)

    monkeypatch.setattr(runner, "ask", counting)
    runner.run(output=tmp_path, only=["job_exit_code"], jobs=1)
    assert len(calls) == 1
    runner.run(output=tmp_path, only=["job_exit_code"], jobs=1)
    assert len(calls) == 1
