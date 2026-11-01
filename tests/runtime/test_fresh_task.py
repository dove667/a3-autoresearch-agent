"""A retained Harness starts a new task without consuming its source checkpoint."""

import json

import pytest

from runtime.local.candidate_versions import CandidateVersions
from runtime.local.continuation import load_run_result, prepare_fresh_task, publish_run_result


def test_repeated_new_tasks_copy_the_same_immutable_checkpoint(tmp_path):
    source = tmp_path / "phase-one"
    workspace = source / "workspace"
    workspace.mkdir(parents=True)
    original = json.dumps([{"role": "user", "content": "old task"}])
    files = {
        "agent.py": "print('unchanged harness')",
        "checkpoint.json": original,
        "memory/lessons.jsonl": '{"lesson":"keep me"}\n',
    }
    for name, value in files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    (workspace / "continuation.json").write_text(
        json.dumps(
            {
                "command": ["python", "agent.py", "--resume"],
                "files": list(files),
            }
        )
    )
    versions = CandidateVersions(source / "versions")
    publish_run_result(source, workspace, versions, "main", None)
    retained_before = (source / "retained.json").read_bytes()
    first, info = prepare_fresh_task(load_run_result(source), "new goal")
    second, _ = prepare_fresh_task(load_run_result(source), "another goal")
    assert json.loads(first.files["checkpoint.json"]) == [{"role": "user", "content": "new goal"}]
    first.files["checkpoint.json"] = "later state"
    assert json.loads(second.files["checkpoint.json"])[0]["content"] == "another goal"
    assert load_run_result(source).files == files
    assert (source / "retained.json").read_bytes() == retained_before
    assert first.files["agent.py"] == files["agent.py"]
    assert first.files["memory/lessons.jsonl"] == files["memory/lessons.jsonl"]
    assert info["previous_message_count"] == 1
    assert info["mode"] == "fresh"


def test_unknown_checkpoint_format_requires_an_explicit_new_task_entry(tmp_path):
    from runtime.local.continuation import HarnessStart

    start = HarnessStart(["python", "custom.py"], {"checkpoint.json": '{"opaque": true}'})
    with pytest.raises(ValueError, match="checkpoint"):
        prepare_fresh_task(start, "new task")


def test_remote_options_are_host_configured_and_shell_quoted(monkeypatch):
    import shlex
    import subprocess

    from runtime.local import launch

    values = {
        "A3_REMOTE_CONTROLLER": "/project path/remote_jobs.py",
        "A3_REMOTE_HOST": "test-host",
        "A3_REMOTE_RUN_ROOT": "/data/runtime",
        "A3_REMOTE_IMAGE": "test-image",
        "A3_REMOTE_MAX_JOBS": "4",
        "A3_REMOTE_JOB_TIMEOUT": "0",
        "A3_REMOTE_RUN_STORAGE_BYTES": "50000000000",
        "A3_REMOTE_MODELS_ROOT": "/project path/models",
        "A3_REMOTE_DATASETS_ROOT": "/project path/datasets",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    calls = []
    monkeypatch.setattr(
        launch.subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append(command) or subprocess.CompletedProcess(command, 0, "{}", "")
        ),
    )
    launch.remote_call("health", {})
    arguments = shlex.split(calls[0][-1])
    assert arguments[-1] == "health"
    assert arguments[arguments.index("--max-jobs") + 1] == "4"
    assert arguments[arguments.index("--timeout") + 1] == "0"
    assert arguments[arguments.index("--run-storage-bytes") + 1] == "50000000000"
    assert arguments[arguments.index("--models-root") + 1] == "/project path/models"


def test_local_storage_does_not_count_source_links_or_other_runs(tmp_path):
    from runtime.local.launch import local_storage_bytes

    run = tmp_path / "run"
    run.mkdir()
    (run / "checkpoint.json").write_bytes(b"1234")
    other = tmp_path / "other"
    other.mkdir()
    (other / "large").write_bytes(b"56789")
    (run / "history").symlink_to(other, target_is_directory=True)
    assert local_storage_bytes(run) == 4


def test_host_reports_storage_independently_of_model_calls(tmp_path):
    import threading

    from fastapi.testclient import TestClient

    from runtime.local.host_gateway import create_app

    reported = threading.Event()
    app = create_app(
        model_name="test",
        sessions={},
        forward_model=lambda **kwargs: pytest.fail("model call"),
        remote_call=lambda *args: {},
        usage_log=tmp_path / "usage.jsonl",
        resource_monitor=reported.set,
    )
    with TestClient(app):
        assert reported.wait(timeout=2)
