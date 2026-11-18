"""The benchmark tool owns the budget and the launcher that reaches the GPU."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from a3_research_agent.harness import job_tools

CPU_ID = "a" * 32
GPU_ID = "b" * 32
CHECKPOINT = "/data1/zsh/checkpoints/020000/pretrained_model"


def prepare_request():
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST":
            return {"job_id": CPU_ID, "status": "queued"}
        return {"job_id": CPU_ID, "status": "completed", "exit_code": 0}

    return calls, request


def preflight(calls):
    return next(body for method, _, body in calls if method == "POST")


def test_benchmark_assembles_the_fixed_runner_and_task_budget(tmp_path):
    calls, request = prepare_request()
    result = job_tools.dispatch(
        "benchmark",
        {"checkpoint": CHECKPOINT, "task": "2x10", "profile": "medium", "episodes": 5},
        request,
        workspace=tmp_path,
    )
    assert result["prepared_id"].startswith("prepared-")
    assert result["preflight_job_id"] == CPU_ID
    assert result["max_steps"] == 3200
    assert re.fullmatch(
        r"2x10-medium-5ep-seed10000-[a-f0-9]{8}/seed-10000\.json",
        result["report"],
    )
    body = preflight(calls)
    command = body["command"]
    assert body["gpu"] is False  # the frozen production job is submitted separately
    assert command[:2] == ["env", "A3_RESEARCH_PREFLIGHT=1"]
    assert command[2:4] == ["python", "benchmark_runner.py"]
    assert command[command.index("--policy") + 1] == "act:" + CHECKPOINT
    assert command[command.index("--episodes") + 1] == "5"
    assert command[command.index("--seed-start") + 1] == "10000"
    assert command[command.index("--max-steps") + 1] == "3200"
    assert command[command.index("--output-dir") + 1] == result["report"].split("/seed-")[0]
    assert body["name"] == "2x10-medium-5ep"
    attachment = body["workspace_files"][0]
    assert attachment["path"] == "benchmarks/a3_benchmark_runner.py"
    assert attachment["destination"] == "benchmark_runner.py"
    runner = Path(job_tools.__file__).with_name("benchmark_runner.py")
    copied = tmp_path / "benchmarks/a3_benchmark_runner.py"
    assert copied.read_text() == runner.read_text()
    assert attachment["sha256"] == hashlib.sha256(copied.read_bytes()).hexdigest()


def test_benchmark_defaults_2x5_to_500_and_accepts_a_larger_budget(tmp_path):
    calls, request = prepare_request()
    five = job_tools.dispatch(
        "benchmark", {"checkpoint": CHECKPOINT, "task": "2x5"}, request, workspace=tmp_path
    )
    assert five["max_steps"] == 500
    assert five["prepared_id"] != five["preflight_job_id"]
    body = preflight(calls)
    assert body["command"][body["command"].index("--max-steps") + 1] == "500"
    assert body["name"] == "2x5-basic-5ep"

    calls, request = prepare_request()
    raised = job_tools.dispatch(
        "benchmark",
        {"checkpoint": CHECKPOINT, "task": "2x10", "max_steps": 6400, "policy": "smolvla"},
        request,
        workspace=tmp_path,
    )
    assert raised["max_steps"] == 6400
    command = preflight(calls)["command"]
    assert command[command.index("--policy") + 1] == "smolvla:" + CHECKPOINT
    assert command[command.index("--max-steps") + 1] == "6400"


def test_benchmark_rejects_a_short_explicit_budget(tmp_path):
    calls, request = prepare_request()
    result = job_tools.dispatch(
        "benchmark",
        {"checkpoint": CHECKPOINT, "task": "2x10", "max_steps": 500},
        request,
        workspace=tmp_path,
    )
    assert result["error"] == "benchmark_budget_too_small"
    assert "3200" in result["hint"]
    assert calls == []


def test_benchmark_requires_the_bare_absolute_checkpoint(tmp_path):
    calls, request = prepare_request()
    for checkpoint in ("relative/ckpt", "act:" + CHECKPOINT):
        result = job_tools.dispatch(
            "benchmark", {"checkpoint": checkpoint, "task": "2x5"}, request, workspace=tmp_path
        )
        assert result["error"] == "invalid_checkpoint"
    assert calls == []


def test_benchmark_rejects_a_symlinked_runner_directory(tmp_path):
    (tmp_path / "target").mkdir()
    (tmp_path / "benchmarks").symlink_to(tmp_path / "target", target_is_directory=True)
    calls, request = prepare_request()
    result = job_tools.dispatch(
        "benchmark", {"checkpoint": CHECKPOINT, "task": "2x5"}, request, workspace=tmp_path
    )
    assert result["error"] == "invalid_benchmark_path"
    assert calls == []


def submit_request():
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        return {"job_id": CPU_ID, "status": "queued"}

    return calls, request


def test_budget_policy_rejects_500_for_2x10_on_the_submission_path(tmp_path):
    command = [
        "python",
        "-m",
        "a3_dual_arm_sim.workflows.benchmark_cookie_batch_cli",
        "--task",
        "2x10",
        "--max-steps",
        "500",
    ]
    calls, request = submit_request()
    result = job_tools.dispatch("job_submit", {"command": command}, request, workspace=tmp_path)
    assert result["error"] == "benchmark_budget_too_small"
    assert "3200" in result["hint"] and "500" in result["hint"]
    assert calls == []


def test_budget_policy_rejects_an_omitted_budget_and_accepts_3200(tmp_path):
    base = [
        "python",
        "-m",
        "a3_dual_arm_sim.workflows.benchmark_cookie_batch_cli",
        "--task",
        "2x10",
    ]
    calls, request = submit_request()
    omitted = job_tools.dispatch("job_submit", {"command": base}, request, workspace=tmp_path)
    assert omitted["error"] == "benchmark_budget_too_small"
    assert "defaults to 1000" in omitted["hint"]

    calls, request = submit_request()
    accepted = job_tools.dispatch(
        "job_submit", {"command": [*base, "--max-steps", "3200"]}, request, workspace=tmp_path
    )
    assert accepted["job_id"] == CPU_ID
    assert len(calls) == 1


def test_budget_policy_reads_shell_strings_and_equals_forms(tmp_path):
    cases = [
        'python -m a3_dual_arm_sim.workflows.benchmark_cookie_batch_cli --task 2x10 --max-steps 500',
        'python -m a3_dual_arm_sim.workflows.benchmark_cookie_batch_cli --task=2x10 --max-steps=500',
        'python -m a3_dual_arm_sim.workflows.benchmark_cookie_batch_cli --task 2×10 --max-steps 500',
    ]
    for shell in cases:
        calls, request = submit_request()
        result = job_tools.dispatch(
            "job_submit", {"command": ["bash", "-lc", shell]}, request, workspace=tmp_path
        )
        assert result["error"] == "benchmark_budget_too_small", (shell, result)
        assert "3200" in result["hint"]
        assert calls == []


def test_a_relative_script_must_be_attached_to_the_job(tmp_path):
    calls, request = submit_request()
    missing = job_tools.dispatch(
        "job_submit", {"command": ["python", "run.py"]}, request, workspace=tmp_path
    )
    assert missing["error"] == "script_not_attached"
    assert "can't open file" in missing["hint"]
    assert calls == []

    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/run.py").write_text("print(1)")
    calls, request = submit_request()
    attached = job_tools.dispatch(
        "job_submit",
        {
            "command": ["python", "scripts/run.py"],
            "attachments": [{"path": "scripts/run.py"}],
        },
        request,
        workspace=tmp_path,
    )
    assert attached["job_id"] == CPU_ID
    assert calls[0][2]["workspace_files"][0]["path"] == "scripts/run.py"


def test_a_relative_script_after_cd_is_rejected_even_when_attached(tmp_path):
    (tmp_path / "run.py").write_text("print(1)")
    calls, request = submit_request()
    result = job_tools.dispatch(
        "job_submit",
        {
            "command": ["bash", "-lc", 'cd "$A3_OUTPUTS" && python run.py'],
            "attachments": [{"path": "run.py"}],
        },
        request,
        workspace=tmp_path,
    )
    assert result["error"] == "script_path_outside_job_workspace"
    assert "A3_WORKSPACE" in result["hint"]
    assert calls == []


def test_output_flags_cannot_write_into_the_shared_a3_checkout(tmp_path):
    cases = [
        ["python", "-m", "bench", "--diagnostic-dir", "diag-seed-1"],
        ["python", "-m", "bench", "--diagnostic-dir", "/data1/zsh/a3_dual_arm_sim/diag-seed-1"],
        ["python", "-m", "bench", "--output=/data1/zsh/a3_dual_arm_sim/report.json"],
        ["python", "-m", "bench", "--output-dir", "/data1/zsh/a3_dual_arm_sim/out"],
    ]
    for command in cases:
        calls, request = submit_request()
        result = job_tools.dispatch("job_submit", {"command": command}, request, workspace=tmp_path)
        assert result["error"] == "output_outside_job_area", (command, result)
        assert "A3_OUTPUTS" in result["hint"]
        assert calls == []


def test_job_outputs_under_the_expanded_job_area_are_allowed(tmp_path):
    calls, request = submit_request()
    result = job_tools.dispatch(
        "job_submit",
        {
            "command": [
                "bash",
                "-lc",
                'python -m bench --diagnostic-dir "$A3_OUTPUTS/diag-seed-1"',
            ]
        },
        request,
        workspace=tmp_path,
    )
    assert result["job_id"] == CPU_ID
    assert len(calls) == 1


def test_benchmark_tool_is_registered_and_catalogued():
    from a3_research_agent.harness import tool_catalog

    assert "benchmark" in job_tools.NAMES
    assert tool_catalog.BY_NAME["benchmark"]["parameters"]["required"] == ["checkpoint", "task"]
    assert "3200" in tool_catalog.BY_NAME["benchmark"]["description"]


def test_benchmark_result_is_json_serializable(tmp_path):
    _calls, request = prepare_request()
    result = job_tools.dispatch(
        "benchmark", {"checkpoint": CHECKPOINT, "task": "2x5"}, request, workspace=tmp_path
    )
    assert json.loads(json.dumps(result))["max_steps"] == 500


def runner_args(**overrides):
    from a3_research_agent.harness import benchmark_runner

    values = {
        "task": "2x10",
        "policy": "act:" + CHECKPOINT,
        "profile": "medium",
        "episodes": 1,
        "seed_start": 10000,
        "max_steps": 3200,
        "output_dir": "cell",
    }
    values.update(overrides)
    argv = [
        "--task",
        values["task"],
        "--policy",
        values["policy"],
        "--output-dir",
        values["output_dir"],
    ]
    for key in ("profile", "episodes", "seed_start", "max_steps"):
        argv += ["--" + key.replace("_", "-"), str(values[key])]
    return benchmark_runner, benchmark_runner.parser().parse_args(argv)


def test_runner_forwards_to_the_deployed_entry_with_the_job_area(tmp_path, monkeypatch):
    benchmark_runner, args = runner_args()
    assert benchmark_runner.budget(args) == 3200
    monkeypatch.setenv("A3_A3", "/remote/a3")
    call = benchmark_runner.command(args, 3200, tmp_path / "out/seed-10000.json", "/venv/python")
    assert call["cwd"] == "/remote/a3"
    assert call["arguments"][0] == "/venv/python"
    assert call["arguments"][1:3] == ["-m", "a3_dual_arm_sim.workflows.benchmark_cookie_batch_cli"]
    assert call["arguments"][call["arguments"].index("--device") + 1] == "cuda"
    assert call["arguments"][call["arguments"].index("--max-steps") + 1] == "3200"
    assert call["arguments"][call["arguments"].index("--n-action-steps") + 1] == "25"
    assert str(tmp_path / "out/seed-10000.json") == call["arguments"][-1]
    assert call["env"]["MUJOCO_GL"] == "egl"


def test_runner_budget_and_output_directory_are_bounded(tmp_path, monkeypatch):
    benchmark_runner, args = runner_args()
    assert benchmark_runner.budget(args) == 3200
    short = runner_args(max_steps=500)[1]
    with pytest.raises(SystemExit, match="at least 3200"):
        benchmark_runner.budget(short)
    monkeypatch.setenv("A3_OUTPUTS", str(tmp_path / "outputs"))
    call = benchmark_runner.parser().parse_args(
        [
            "--task",
            "2x5",
            "--policy",
            "act:" + CHECKPOINT,
            "--output-dir",
            "cell",
        ]
    )
    seen = {}

    def fake_run(arguments, cwd, env, check):
        seen.update(arguments=arguments, cwd=cwd, env=env, check=check)
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(benchmark_runner.subprocess, "run", fake_run)
    assert benchmark_runner.run(call) == 0
    assert (tmp_path / "outputs/cell").is_dir()
    assert seen["check"] is False

    call = benchmark_runner.parser().parse_args(
        [
            "--task",
            "2x5",
            "--policy",
            "act:" + CHECKPOINT,
            "--output-dir",
            "/tmp/outside",
        ]
    )
    with pytest.raises(SystemExit, match="below A3_OUTPUTS"):
        benchmark_runner.run(call)


def test_repeating_the_same_benchmark_call_is_idempotent(tmp_path):
    calls, request = prepare_request()
    first = job_tools.dispatch(
        "benchmark", {"checkpoint": CHECKPOINT, "task": "2x10"}, request, workspace=tmp_path
    )
    second = job_tools.dispatch(
        "benchmark", {"checkpoint": CHECKPOINT, "task": "2x10"}, request, workspace=tmp_path
    )
    assert second["prepared_id"] == first["prepared_id"]
    assert second["report"] == first["report"]
    assert second["max_steps"] == first["max_steps"] == 3200
    assert len([body for method, _, body in calls if method == "POST"]) == 1


def test_job_prepare_also_rejects_a_short_benchmark_budget(tmp_path):
    calls, request = submit_request()
    result = job_tools.dispatch(
        "job_prepare",
        {
            "command": [
                "python",
                "-m",
                "a3_dual_arm_sim.workflows.benchmark_cookie_batch_cli",
                "--task",
                "2x10",
                "--max-steps",
                "500",
            ],
            "gpu": True,
        },
        request,
        workspace=tmp_path,
    )
    assert result["error"] == "benchmark_budget_too_small"
    assert "3200" in result["hint"]
    assert calls == []
