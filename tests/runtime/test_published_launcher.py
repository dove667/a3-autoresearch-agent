"""The public launcher loads published native source."""

import json
import sys
from pathlib import Path

import pytest
import test_in_place_release

from a3_research_agent.cli import launch
from a3_research_agent.host.published_harness import COMMAND
from a3_research_agent.host.remote_client import RemoteCommandError

published_run = test_in_place_release.published_run


def runtime_for(root, calls, monkeypatch):
    monkeypatch.setattr(launch, "checkout_root", lambda: root)
    monkeypatch.setattr(launch, "resolve_project_root", lambda value: Path(value))
    monkeypatch.setattr(launch, "load_dotenv", lambda *args, **kwargs: calls.append("config"))
    monkeypatch.setattr(
        launch,
        "remote_call",
        lambda *args: {"status": "ok", "protocol_version": 2, "backend": "process"},
    )
    monkeypatch.setattr(launch, "reference_commit", lambda *args: "source-commit")
    monkeypatch.setattr(launch, "launch_run", lambda *args: calls.append(args))


def configure(monkeypatch, root):
    values = {
        "MODEL_NAME": "fixture/model",
        "A3_PROJECT_ROOT": str(root),
        "A3_REMOTE_HOST": "fixture-host",
        "A3_REMOTE_CONTROLLER": "/data/controller",
        "A3_REMOTE_RUN_ROOT": "/data/jobs",
        "A3_REMOTE_DATA_ROOT": "/data",
        "A3_REMOTE_PROCESS_PYTHON": "/data/python",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("A3_NIGHT_WINDOW_CHILD", raising=False)
    monkeypatch.delenv("A3_NIGHT_WINDOW_TARGET", raising=False)


def test_fresh_published_commit_launches_native_research(published_run, monkeypatch):
    root, _, _, _, commit, *_ = published_run
    calls = []
    configure(monkeypatch, root)
    monkeypatch.setattr(
        sys,
        "argv",
        ["launch", "--harness-commit", commit],
    )
    runtime_for(root, calls, monkeypatch)
    launch.launch_main()
    args, run, _, start = calls[-1]
    assert "SOUL.md" in args.input_proposal["contents"]
    assert "goal.md" not in args.input_proposal["contents"]
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


def test_unreachable_controller_keeps_the_ssh_diagnostic(published_run, monkeypatch):
    root, *_ = published_run
    calls = []
    configure(monkeypatch, root)
    monkeypatch.setattr(sys, "argv", ["launch", "--check"])
    runtime_for(root, calls, monkeypatch)

    def unreachable(*args):
        raise RemoteCommandError(
            255, ["ssh"], "", "ssh: connect to host fixture-host port 22: Network is unreachable"
        )

    monkeypatch.setattr(launch, "remote_call", unreachable)
    with pytest.raises(SystemExit) as error:
        launch.launch_main()
    assert "Remote Controller is unreachable" in error.value.code
    assert "Network is unreachable" in error.value.code


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
        ["launch", "--harness-commit", previous],
    )
    with pytest.raises(SystemExit) as error:
        runtime_for(root, calls, monkeypatch)
        launch.launch_main()
    assert error.value.code == 2 and not calls


def test_retired_task_input_option_is_rejected_before_deployment(published_run, monkeypatch):
    root, _, _, _, commit, *_ = published_run
    calls = []
    monkeypatch.setattr(
        sys, "argv", ["launch", "--harness-commit", commit, "--agent-inputs-dir", "/old/task"]
    )
    runtime_for(root, calls, monkeypatch)
    with pytest.raises(SystemExit) as error:
        launch.launch_main()
    assert error.value.code == 2 and not calls


def test_invalid_current_prompt_is_rejected_before_deployment(published_run, monkeypatch):
    root, _, _, _, commit, *_ = published_run
    (root / "prompt/SOUL.md").write_text("Missing version")
    calls = []
    monkeypatch.setattr(sys, "argv", ["launch", "--harness-commit", commit])
    runtime_for(root, calls, monkeypatch)
    with pytest.raises(SystemExit) as error:
        launch.launch_main()
    assert error.value.code == 2 and not calls


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
