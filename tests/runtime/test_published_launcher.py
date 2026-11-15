"""The public launcher uses published native source without touching Docker."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import test_in_place_release

from a3_research_agent.cli import launch
from a3_research_agent.host.published_harness import COMMAND

published_run = test_in_place_release.published_run


def runtime_for(root, calls, monkeypatch):
    monkeypatch.setattr(launch, "checkout_root", lambda: root)
    monkeypatch.setattr(launch, "resolve_project_root", lambda value: Path(value))
    monkeypatch.setattr(launch, "load_dotenv", lambda *args, **kwargs: calls.append("config"))
    monkeypatch.setattr(
        launch, "remote_call", lambda *args: {"status": "ok", "protocol_version": 1}
    )
    monkeypatch.setattr(launch, "git", lambda *args: "source-commit")
    monkeypatch.setattr(launch, "launch_run", lambda *args: calls.append(args))


def configure(monkeypatch, root):
    original_run = subprocess.run

    def without_docker(command, **options):
        if command[0] == "docker":
            pytest.fail("Published launcher accessed Docker")
        return original_run(command, **options)

    monkeypatch.setattr(subprocess, "run", without_docker)
    values = {
        "MODEL_NAME": "fixture/model",
        "A3_PROJECT_ROOT": str(root),
        "A3_REMOTE_HOST": "fixture-host",
        "A3_REMOTE_CONTROLLER": "/data/controller",
        "A3_REMOTE_RUN_ROOT": "/data/jobs",
        "A3_REMOTE_BACKEND": "process",
        "A3_REMOTE_DATA_ROOT": "/data",
        "A3_REMOTE_PROCESS_PYTHON": "/data/python",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("A3_NIGHT_WINDOW_RUN_ID", raising=False)
    monkeypatch.delenv("A3_NIGHT_WINDOW_CHILD", raising=False)
    monkeypatch.delenv("A3_NIGHT_WINDOW_TARGET", raising=False)


def test_fresh_published_commit_launches_explicit_task_without_docker(published_run, monkeypatch):
    root, _, _, _, commit, *_ = published_run
    calls = []
    configure(monkeypatch, root)
    monkeypatch.setattr(
        sys,
        "argv",
        ["launch", "--harness-commit", commit, "--agent-inputs-dir", str(root / "task")],
    )
    runtime_for(root, calls, monkeypatch)
    launch.launch_main()
    _args, run, _, start = calls[-1]
    assert run.parent == root / "trajectories"
    assert start.harness_commit == commit and start.command == COMMAND
    assert start.files == {"checkpoint.json": "[]"}


def test_check_reads_native_configuration_without_an_execution_start(
    published_run, monkeypatch, capsys
):
    root, *_ = published_run
    calls = []
    configure(monkeypatch, root)
    monkeypatch.setattr(sys, "argv", ["launch", "--check"])
    runtime_for(root, calls, monkeypatch)
    launch.launch_main()
    assert json.loads(capsys.readouterr().out)["local_execution"] == "process"
    assert calls == ["config"]


@pytest.mark.parametrize("flags", [[], ["--verify-deployment"]])
def test_retired_cold_start_cannot_access_deployment(published_run, monkeypatch, flags):
    root, *_ = published_run
    calls = []
    monkeypatch.setattr(sys, "argv", ["launch", *flags])
    with pytest.raises(SystemExit) as error:
        runtime_for(root, calls, monkeypatch)
        launch.launch_main()
    assert error.value.code == 2 and not calls


def test_generated_source_package_requires_published_source_before_deployment(
    published_run, tmp_path, monkeypatch
):
    root, *_ = published_run
    package = tmp_path / "generated"
    package.mkdir()
    (package / "agent.py").write_text("print('generated')")
    (package / "run.json").write_text(
        json.dumps(
            {
                "command": ["python", "agent.py"],
                "files": ["agent.py"],
            }
        )
    )
    calls = []
    monkeypatch.setattr(sys, "argv", ["launch", "--run-harness", str(package)])
    with pytest.raises(SystemExit) as error:
        runtime_for(root, calls, monkeypatch)
        launch.launch_main()
    assert error.value.code == 2 and not calls


def test_old_published_source_needs_a_release_before_any_run_is_created(published_run, monkeypatch):
    root, _, _, previous, *_ = published_run
    calls = []
    monkeypatch.setattr(
        sys,
        "argv",
        ["launch", "--harness-commit", previous, "--agent-inputs-dir", str(root / "task")],
    )
    with pytest.raises(SystemExit) as error:
        runtime_for(root, calls, monkeypatch)
        launch.launch_main()
    assert error.value.code == 2 and not calls


def test_fresh_published_source_accepts_complete_custom_inputs(
    published_run, tmp_path, monkeypatch
):
    root, _, _, _, commit, *_ = published_run
    inputs = tmp_path / "custom-task"
    inputs.mkdir()
    (inputs / "protocol.md").write_text("Version: 1.0.0\n\nReport reproducible results.\n")
    (inputs / "goal.md").write_text(
        "Version: 1.0.0\n\nInvestigate the selected research question.\n"
    )
    (inputs / "acceptance.md").write_text(
        "Version: 1.0.0\n\n## reproducible. Link source and result\n"
    )
    calls = []
    configure(monkeypatch, root)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "launch",
            "--harness-commit",
            commit,
            "--agent-inputs-dir",
            str(inputs),
        ],
    )
    runtime_for(root, calls, monkeypatch)
    original = {path.name: path.read_bytes() for path in inputs.iterdir()}
    launch.launch_main()
    args, _, _, start = calls[-1]
    assert args.custom_input_proposal["contents"]["goal.md"].startswith("Version: 1.0.0")
    assert start.harness_commit == commit and json.loads(start.files["checkpoint.json"]) == []
    assert original == {path.name: path.read_bytes() for path in inputs.iterdir()}


def test_retired_artifact_run_directory_is_rejected_before_deployment(published_run, monkeypatch):
    root, run, *_ = published_run
    retired = root / "artifacts/rsi" / run.name
    retired.parent.mkdir(parents=True)
    run.rename(retired)
    calls = []
    monkeypatch.setattr(sys, "argv", ["launch", "--resume-from", str(retired)])
    runtime_for(root, calls, monkeypatch)
    with pytest.raises(SystemExit) as error:
        launch.launch_main()
    assert error.value.code == 2 and not calls
