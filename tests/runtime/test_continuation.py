import json

import pytest

from a3_research_agent.host.runs.continuation import (
    load_run_result,
    publish_run_result,
    resolve_harness_start,
)
from a3_research_agent.host.sessions.versions import CandidateVersions


@pytest.mark.parametrize("count,size", [(129, 1), (1, 21 * 1024 * 1024)])
def test_large_local_result_can_be_published_and_loaded_without_uploading(tmp_path, count, size):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    names = [f"data_{index}.txt" for index in range(count)]
    for name in names:
        (workspace / name).write_bytes(b"x" * size)
    (workspace / "continuation.json").write_text(json.dumps({"command": ["true"], "files": names}))
    versions = CandidateVersions(tmp_path / "versions")
    result = publish_run_result(tmp_path, workspace, versions, "owner", None)
    assert result["status"] == "ready"
    (workspace / names[0]).write_text("later edit")
    start = load_run_result(tmp_path)
    assert len(start.files) == count
    assert start.files[names[0]] == "x" * size
    # Importing an authorized local snapshot must not reapply HTTP upload limits.
    imported = versions.create_local("next-owner", start.files)
    replay = versions.checkout("next-owner", imported["version_id"], "next")
    assert (replay / names[0]).read_bytes() == b"x" * size


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


@pytest.mark.parametrize("with_manifest", [False, True])
def test_unpublished_workspace_files_are_not_a_run_result(tmp_path, with_manifest):
    run = tmp_path / "run"
    final = run / "workspace/run/final"
    final.mkdir(parents=True)
    (run / "startup.json").write_text("{}")
    (final / "entry.py").write_bytes(b"print('retained')\n")
    (final / "rejected-candidate.py").write_text("raise RuntimeError")
    if with_manifest:
        (final / "run.json").write_text(
            json.dumps({"command": ["python", "entry.py"], "files": ["entry.py"]})
        )
    with pytest.raises(ValueError, match="retained"):
        load_run_result(run)


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


def test_resume_retained_run_requires_explicit_checkpoint_entry(tmp_path):
    from a3_research_agent.host.runs.continuation import resolve_resume_start

    (tmp_path / "startup.json").write_text(
        json.dumps({"mode": "retained-harness", "session_id": "agent", "version_id": "v1"})
    )
    workspace = tmp_path / "candidates/agent/workspace"
    workspace.mkdir(parents=True)
    with pytest.raises(ValueError, match="resume.json"):
        resolve_resume_start(tmp_path)
    (workspace / "entry.py").write_text("pass")
    (workspace / "checkpoint.json").write_text('{"messages": []}')
    (workspace / "resume.json").write_text(
        json.dumps(
            {
                "command": ["python", "entry.py", "--resume", "checkpoint.json"],
                "files": ["entry.py", "checkpoint.json"],
            }
        )
    )
    start = resolve_resume_start(tmp_path)
    assert start.command[-2:] == ["--resume", "checkpoint.json"]
    assert "checkpoint.json" in start.files


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

    from a3_research_agent.host import bootstrap as launch

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


def test_cold_start_resume_selects_promoted_main_checkpoint(tmp_path):
    from a3_research_agent.host.runs.continuation import resolve_resume_start

    (tmp_path / "startup.json").write_text(json.dumps({"mode": "rsi", "run_id": "run-original"}))
    (tmp_path / "evolution.json").write_text(json.dumps({"main_owner": "candidate-promoted"}))
    workspace = tmp_path / "candidates/candidate-promoted/workspace"
    workspace.mkdir(parents=True)
    (workspace / "entry.py").write_text("pass")
    (workspace / "checkpoint.json").write_text('{"messages": []}')
    (workspace / "resume.json").write_text(
        json.dumps(
            {
                "command": ["python", "entry.py", "--resume", "checkpoint.json"],
                "files": ["entry.py", "checkpoint.json"],
            }
        )
    )
    start = resolve_resume_start(tmp_path)
    assert start.command[-1] == "checkpoint.json"
    assert start.previous_run == tmp_path


def test_resume_loads_checkpoint_from_host_managed_worktree_link(tmp_path):
    from a3_research_agent.host.runs.continuation import resolve_resume_start

    owner = "candidate-promoted"
    files = {
        "entry.py": "pass",
        "checkpoint.json": '[{"role":"user","content":"existing context"}]',
        "resume.json": json.dumps(
            {
                "command": ["python", "entry.py", "--resume"],
                "files": ["entry.py", "checkpoint.json"],
            }
        ),
    }
    versions = CandidateVersions(tmp_path / "versions")
    version = versions.create(owner, files)
    worktree = versions.checkout(owner, version["version_id"], owner)
    directory = tmp_path / "candidates" / owner
    directory.mkdir(parents=True)
    (directory / "workspace").symlink_to(worktree, target_is_directory=True)
    (tmp_path / "startup.json").write_text(json.dumps({"run_id": "original", "mode": "rsi"}))
    (tmp_path / "evolution.json").write_text(json.dumps({"main_owner": owner}))
    start = resolve_resume_start(tmp_path)
    assert start.files["checkpoint.json"] == files["checkpoint.json"]
    assert start.command == ["python", "entry.py", "--resume"]
    (directory / "workspace").unlink()
    (directory / "workspace").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="worktree"):
        resolve_resume_start(tmp_path)
