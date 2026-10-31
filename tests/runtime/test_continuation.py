import json

import pytest

from runtime.local.candidate_versions import CandidateVersions
from runtime.local.continuation import load_run_result, publish_run_result, resolve_harness_start


def test_explicit_run_reuses_retained_result_regardless_of_latest_pointer(tmp_path):
    previous = tmp_path / "run-previous"
    workspace = previous / "workspace"
    workspace.mkdir(parents=True)
    files = {"next.py": "print('evolved')\r\n", "memory/lessons.json": '{"failed": true}'}
    for name, value in files.items():
        target = workspace / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(value.encode())
    (workspace / "continuation.json").write_text(
        json.dumps(
            {
                "command": ["python", "next.py"],
                "files": list(files),
            }
        )
    )
    versions = CandidateVersions(previous / "versions")
    initial = versions.create("agent", {"entry.py": "pass"})
    result = publish_run_result(previous, workspace, versions, "agent", initial["version_id"])
    assert result["status"] == "ready"
    # The selected snapshot stays usable even after workspace edits.
    (workspace / "next.py").write_text("raise RuntimeError('later edit')")
    pointer = tmp_path / "latest-run.txt"
    pointer.write_text(str(tmp_path / "some-other-run"))
    start = resolve_harness_start(previous)
    assert start.command == ["python", "next.py"]
    assert start.files == files
    assert start.previous_run == previous


def test_legacy_final_entry_is_loaded_without_manually_prepared_package(tmp_path):
    run = tmp_path / "run-legacy"
    final = run / "workspace/run/final"
    final.mkdir(parents=True)
    (run / "startup.json").write_text("{}")
    (final / "entry.py").write_bytes(b"print('retained')\n")
    (final / "rejected-candidate.py").write_text("raise RuntimeError")
    start = load_run_result(run)
    assert start.files == {"entry.py": "print('retained')\n"}
    assert start.previous_run == run


def test_missing_selection_does_not_guess_newest_candidate_or_restart_from_zero(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "startup.json").write_text("{}")
    with pytest.raises(ValueError, match="retained"):
        load_run_result(run)
    versions = CandidateVersions(run / "versions")
    result = publish_run_result(run, run, versions, "agent", None)
    assert result["status"] == "missing"
    with pytest.raises(ValueError, match="retained"):
        load_run_result(run)


def test_selection_rejects_escaped_files_and_missing_version_objects(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "continuation.json").write_text(
        json.dumps({"command": ["true"], "files": ["../secret"]})
    )
    versions = CandidateVersions(run / "versions")
    with pytest.raises(ValueError):
        publish_run_result(run, run, versions, "agent", None)
    (run / "retained.json").write_text(
        json.dumps({"status": "ready", "version_id": "unknown", "command": ["true"]})
    )
    with pytest.raises(ValueError, match="retained"):
        load_run_result(run)


def test_resume_retained_run_keeps_executed_harness_when_no_final_selection(tmp_path):
    from runtime.local.continuation import resolve_resume_start

    run = tmp_path / "run"
    versions = CandidateVersions(run / "versions")
    saved = versions.create("agent", {"old.py": "print('in progress')"})
    (run / "startup.json").write_text(
        json.dumps(
            {
                "mode": "retained-harness",
                "version_id": saved["version_id"],
                "session_id": "agent",
            }
        )
    )
    session = run / "candidates/agent"
    session.mkdir(parents=True)
    (session / "session.json").write_text(json.dumps({"command": ["python", "old.py"]}))
    start = resolve_resume_start(run)
    assert start.files == {"old.py": "print('in progress')"}
    assert start.command == ["python", "old.py"]
    assert start.previous_run == run


def test_full_file_capacity_snapshot_is_not_modified_by_restart_context(tmp_path):
    run = tmp_path / "run"
    workspace = run / "workspace"
    workspace.mkdir(parents=True)
    files = {f"module_{i}.py": "pass" for i in range(128)}
    for name, text in files.items():
        (workspace / name).write_text(text)
    (workspace / "continuation.json").write_text(
        json.dumps({"command": ["true"], "files": list(files)})
    )
    versions = CandidateVersions(run / "versions")
    publish_run_result(run, workspace, versions, "owner", None)
    start = load_run_result(run)
    assert start.files == files
    assert versions.create("new-owner", start.files)["file_count"] == 128


def test_run_harness_requires_an_explicit_source_before_starting_resources(monkeypatch, tmp_path):
    import sys

    from runtime.local import launch

    def forbidden(*args, **kwargs):
        raise AssertionError("Live deployment must never be used by CLI parsing tests")

    monkeypatch.setattr(sys, "argv", ["launch", "--run-harness"])
    monkeypatch.setattr(launch, "remote_call", forbidden)
    monkeypatch.setattr(launch.litellm, "responses", forbidden)
    monkeypatch.setenv("A3_RSI_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("MODEL_NAME", "")
    monkeypatch.setenv("MODEL_CLASS", "")
    with pytest.raises(SystemExit) as error:
        launch.main()
    assert error.value.code == 2
