"""The benchmark tool owns the budget and the launcher that reaches the GPU."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from a3_research_agent.harness import job_tools
from a3_research_agent.harness.tool_contracts import ToolInputError

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


def test_act_rejects_a_language_option():
    """ACT reads pixels and state only; a prompt style would imply otherwise."""
    from a3_research_agent.harness import benchmark_policy

    with pytest.raises(Exception) as error:
        benchmark_policy.check_command(
            ["python", "x.py", "--policy", "act:/ckpt", "--prompt-style", "training"]
        )
    assert error.value.code == "prompt_style_not_applicable"


def test_smolvla_may_carry_a_language_option():
    from a3_research_agent.harness import benchmark_policy

    benchmark_policy.check_command(
        ["python", "x.py", "--policy", "smolvla:/ckpt", "--prompt-style", "short"]
    )


def test_runner_omits_prompt_style_for_act():
    from a3_research_agent.harness import benchmark_runner

    args = benchmark_runner.parser().parse_args(
        ["--task", "2x10", "--policy", "act:/ckpt", "--output-dir", "cell"]
    )
    call = benchmark_runner.command(args, 3200, "/out/r.json", "python")
    assert "--prompt-style" not in call["arguments"]

    args = benchmark_runner.parser().parse_args(
        ["--task", "2x10", "--policy", "smolvla:/ckpt", "--prompt-style", "short",
         "--output-dir", "cell"]
    )
    call = benchmark_runner.command(args, 3200, "/out/r.json", "python")
    assert "--prompt-style" in call["arguments"]


def test_dataset_audit_submits_a_cpu_job_and_names_the_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sent = {}

    def request(method, path, body=None, **kwargs):
        sent["method"], sent["path"], sent["body"] = method, path, body
        return {"ok": True, "job_id": "c" * 32}

    from a3_research_agent.harness import job_tools

    result = job_tools.dispatch(
        "dataset_audit",
        {"dataset_root": "/data1/zsh/a3_dual_arm_sim/mixed-2x10-v1/ds-merged"},
        request,
        workspace=str(tmp_path),
    )
    assert sent["path"] == "/jobs" and sent["body"]["gpu"] is False
    assert sent["body"]["command"][:2] == ["python", "a3_dataset_audit_runner.py"]
    assert result["report"].endswith("/audit.json")
    attached = sent["body"]["workspace_files"][0]
    assert attached["destination"] == "a3_dataset_audit_runner.py"
    assert (tmp_path / "benchmarks" / "a3_dataset_audit_runner.py").is_file()


def test_dataset_audit_requires_an_absolute_remote_path(tmp_path):
    from a3_research_agent.harness import job_tools

    result = job_tools.dispatch(
        "dataset_audit",
        {"dataset_root": "datasets/local"},
        lambda *a, **k: {},
        workspace=str(tmp_path),
    )
    assert result["error"] == "invalid_dataset_root"


def test_dataset_audit_runner_reports_layouts_and_warnings(tmp_path, monkeypatch):
    from a3_research_agent.harness import dataset_audit_runner as runner

    root = tmp_path / "ds"
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text(json.dumps({"total_episodes": 2, "total_tasks": 1}))
    (root / "collection_summary.json").write_text(
        json.dumps({"complete_demonstrations": False, "target_layout": "2x10"})
    )
    rows = [
        {"task": "Transfer 20 cookies into the 2x10 target box.", "seed": 20000, "frames": 1000},
        {"task": "Transfer 20 cookies into the 2x10 target box.", "seed": 20001, "frames": 1000},
    ]
    (root / "a3_episode_metadata.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n"
    )
    monkeypatch.setattr(runner, "audit", lambda *a, **k: {"episodes": 2})
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("A3_OUTPUTS", str(outputs))
    runner.main(["--dataset-root", str(root), "--output-dir", "cell"])
    report = json.loads((outputs / "cell" / "audit.json").read_text())
    assert report["metadata"]["layouts"] == {"2x10": 2}
    assert any("only one task layout" in warning for warning in report["warnings"])
    assert any("complete_demonstrations" in warning for warning in report["warnings"])


def test_a3_api_runs_a_cpu_reader_and_names_the_report(tmp_path):
    from a3_research_agent.harness import job_tools

    sent = {}

    def request(method, path, body=None, **kwargs):
        sent["body"] = body
        return {"ok": True, "job_id": "d" * 32}

    result = job_tools.dispatch("a3_api", {"symbols": ["train_act"]}, request, workspace=str(tmp_path))
    assert sent["body"]["gpu"] is False
    assert sent["body"]["command"][:2] == ["python", "a3_api_reader.py"]
    assert result["report"].endswith("/api.json")
    assert (tmp_path / "benchmarks" / "a3_api_reader.py").is_file()


def test_a3_api_reader_reports_definitions_and_cli_defaults(tmp_path, monkeypatch):
    from a3_research_agent.harness import a3_api_runner as reader

    package = tmp_path / "pkg"
    (package / "workflows").mkdir(parents=True)
    (package / "learning.py").write_text(
        "def train_act(dataset_root, dry_run=False,\n              steps=20000):\n    return None\n"
    )
    (package / "workflows" / "benchmark_cookie_batch_cli.py").write_text(
        'parser.add_argument("--max-steps", type=int, default=1000)\n'
        'parser.add_argument("--task", default="2x5")\n'
    )
    monkeypatch.setattr(reader, "package_root", lambda: package)
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("A3_OUTPUTS", str(outputs))
    reader.main(["--output-dir", "api"])
    report = json.loads((outputs / "api" / "api.json").read_text())
    definition = report["definitions"]["train_act"][0]
    assert definition["file"] == "learning.py"
    assert "train_act(dataset_root" in definition["signature"]
    flags = report["cli"][0]["flags"]
    assert {"flag": "--max-steps", "default": "1000"} in flags
    assert {"flag": "--task", "default": '"2x5"'} in flags


def test_benchmark_exposes_two_boxes_quota_and_passes_it_to_the_runner(tmp_path):
    from a3_research_agent.harness import benchmark_runner

    tool = next(tool for tool in job_tools.TOOLS if tool["name"] == "benchmark")
    properties = tool["parameters"]["properties"]
    assert properties.keys() == job_tools.BENCHMARK_FIELDS
    assert set(properties["task"]["enum"]) == set(job_tools.benchmark_policy.TASK_STEPS)
    assert properties["box_quota"]["enum"] == [5, 10]

    calls, request = prepare_request()
    result = job_tools.dispatch(
        "benchmark",
        {"checkpoint": CHECKPOINT, "task": "two-boxes", "box_quota": 10, "episodes": 1},
        request,
        workspace=tmp_path,
    )
    assert result["max_steps"] == 3200
    args = benchmark_runner.parser().parse_args(preflight(calls)["command"][4:])
    assert args.box_quota == 10
    command = benchmark_runner.command(args, 3200, "/out/r.json", "python")["arguments"]
    assert command[command.index("--box-quota") + 1] == "10"


def test_scenario_tasks_carry_their_own_budget_and_score():
    """The six extension scenarios are 2x5-scale; two full boxes follow 2x10."""
    from a3_research_agent.harness import benchmark_policy

    assert benchmark_policy.steps_for("partial-gaps", []) == 1000
    assert benchmark_policy.max_score("prefilled") == 5
    assert benchmark_policy.steps_for("two-boxes", ["--box-quota", "10"]) == 3200
    assert benchmark_policy.max_score("two-boxes", ["--box-quota", "10"]) == 20
    # A 2x5 budget is still refused for a scenario that moves twice the cookies.
    with pytest.raises(ToolInputError):
        benchmark_policy.check_command(
            ["python", "benchmark_cookie_batch_cli.py", "--task", "tilted", "--max-steps", "500"]
        )


def test_benchmark_records_diagnostic_paths_for_each_seed(tmp_path):
    from a3_research_agent.harness import benchmark_runner

    calls, request = prepare_request()
    result = job_tools.dispatch(
        "benchmark",
        {
            "checkpoint": CHECKPOINT,
            "task": "2x10",
            "seed_start": 10003,
            "episodes": 2,
            "diagnostics": True,
        },
        request,
        workspace=tmp_path,
    )
    args = benchmark_runner.parser().parse_args(preflight(calls)["command"][4:])
    report = tmp_path / result["report"]
    command = benchmark_runner.command(args, 3200, report, "python")["arguments"]
    diagnostics = report.parent / "diagnostics"
    assert command[command.index("--diagnostic-dir") + 1] == str(diagnostics)
    for item, seed in zip(result["diagnostics"], (10003, 10004), strict=True):
        episode = diagnostics / f"seed_{seed}"
        assert item == {
            "seed": seed,
            "trace": str((episode / "trace.jsonl").relative_to(tmp_path)),
            "videos": {
                camera: str((episode / f"{camera}.mp4").relative_to(tmp_path))
                for camera in ("front", "left_wrist", "right_wrist")
            },
        }


def test_diagnostic_runner_publishes_report_and_video_together(tmp_path, monkeypatch):
    runner, args = runner_args()
    args.diagnostics = True
    monkeypatch.setenv("A3_OUTPUTS", str(tmp_path))
    attempts = iter([(1, False), (0, False), (0, True)])

    def record(call):
        argv = call["arguments"]
        report = Path(argv[argv.index("--output") + 1])
        diagnostics = Path(argv[argv.index("--diagnostic-dir") + 1])
        assert not (tmp_path / args.output_dir).exists()
        exit_code, complete = next(attempts)
        metadata = {"output": str(report), "diagnostic_dir": str(diagnostics)}
        report.write_text(json.dumps({"run": metadata}))
        diagnostics.mkdir()
        (diagnostics / "manifest.json").write_text(json.dumps(metadata))
        episode = diagnostics / "seed_10000"
        episode.mkdir()
        (episode / "trace.jsonl").write_text("{}\n")
        for camera in ("front", "left_wrist", "right_wrist"):
            if complete or camera != "right_wrist":
                (episode / f"{camera}.mp4").write_bytes(b"video")
        return exit_code

    monkeypatch.setattr(runner, "run_benchmark", record)
    assert runner.run(args) == 1
    assert not (tmp_path / args.output_dir).exists()
    with pytest.raises(SystemExit, match="right_wrist.mp4"):
        runner.run(args)
    assert not (tmp_path / args.output_dir).exists()
    assert runner.run(args) == 0
    report = tmp_path / args.output_dir / "seed-10000.json"
    diagnostics = report.parent / "diagnostics"
    manifest = diagnostics / "manifest.json"
    metadata = {"output": str(report), "diagnostic_dir": str(diagnostics)}
    assert json.loads(report.read_text())["run"] == metadata
    assert json.loads(manifest.read_text()) == metadata
    assert (diagnostics / "seed_10000/front.mp4").is_file()
