"""F-only tasks require successful retained E, not a template declaration."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from a3_research_agent.host import bootstrap as launch
from a3_research_agent.host.inputs import versions as input_versions
from a3_research_agent.host.runs.continuation import HarnessStart


def e_source(tmp_path):
    source = tmp_path / "e-run"
    source.mkdir()
    records = {
        "startup.json": {"phase": "phase-one", "task_profile": "web-extension"},
        "retained.json": {"status": "ready", "version_id": "v1"},
        "outcome.json": {
            "status": "complete",
            "acceptance": {"E": {"status": "passed", "evidence": ["evidence.json"]}},
            "input_manifest": {
                "phase": "phase-one",
                "task_profile": "web-extension",
                "acceptance_checks": ["E"],
            },
        },
    }
    for name, data in records.items():
        (source / name).write_text(json.dumps(data))
    files = {
        "checkpoint.json": json.dumps([{"role": "user", "content": "old E"}] * 199),
        "agent.py": "code",
        "memory/lesson": "keep",
        "task.txt": "old E",
        "progress.md": "complete",
        "trajectory.jsonl": "old events",
        "run-outcome.json": json.dumps(records["outcome.json"]),
    }
    return HarnessStart(["python", "agent.py"], files, source, "v1")


def test_skills_profile_inputs_and_resume(tmp_path):
    root = Path(__file__).resolve().parents[2]
    versions = input_versions.InputVersions(root, tmp_path / "f", task_profile="skills-extension")
    proposal = versions.prepare()
    manifest = versions.activate(proposal, "start")
    assert manifest["acceptance_checks"] == ["F"]
    assert manifest["task_profile"] == "skills-extension"
    assert manifest["files"]["goal.md"]["source"] == "agent-inputs/phase-one-skills/goal.md"
    assert "protocol.md" not in manifest["files"]
    restored = input_versions.InputVersions(root, tmp_path / "f")
    assert restored.task_profile == "skills-extension"
    restored.activate(restored.prepare(), "resume")
    with pytest.raises(ValueError, match="profile"):
        input_versions.InputVersions(root, tmp_path / "f", task_profile="web-extension")
    with pytest.raises(ValueError, match="profile"):
        restored.activate(dict(proposal, task_profile="web-extension"), "update")
    with pytest.raises(ValueError, match="phase-one"):
        input_versions.input_sources("phase-two", "skills-extension")


@pytest.mark.parametrize("feedback", [None, "Approved F notification"])
def test_fresh_f_metadata_state_and_feedback(tmp_path, feedback):
    start = e_source(tmp_path)
    original = dict(start.files)
    before = {p.name: p.read_bytes() for p in start.previous_run.iterdir()}
    fresh, context = launch.prepare_task_start(
        start, "phase-one", "skills-extension", "F goal", initial_feedback=feedback
    )
    messages = json.loads(fresh.files["checkpoint.json"])
    assert messages[0] == {"role": "user", "content": "F goal"}
    assert len(messages) == (2 if feedback else 1)
    if feedback:
        digest = hashlib.sha256(feedback.encode()).hexdigest()
        assert messages[1] == {
            "role": "user",
            "content": f"Initial human feedback (sha256: {digest})\n\n{feedback}",
        }
        assert context["initial_feedback"]["sha256"] == digest
        assert context["initial_feedback"]["mode"] == "initial-user-input"
    else:
        assert "initial_feedback" not in context
    assert context["previous_message_count"] == 199
    assert context["inherited_acceptance"]["checks"] == ["A", "B", "C", "D", "E"]
    assert context["inherited_acceptance"]["status"] == "historical-not-recertified"
    assert fresh.files["task.txt"] == "F goal"
    assert fresh.files["trajectory.jsonl"] == ""
    assert "F is pending" in fresh.files["progress.md"]
    assert json.loads(fresh.files["run-outcome.json"])["status"] == "continue"
    for name in ["task.txt", "progress.md", "trajectory.jsonl", "run-outcome.json"]:
        assert fresh.files["inherited-task-state/skills-extension/" + name] == original[name]
    assert fresh.files["agent.py"] == "code"
    assert fresh.files["memory/lesson"] == "keep"
    assert start.files == original
    assert before == {p.name: p.read_bytes() for p in start.previous_run.iterdir()}


@pytest.mark.parametrize(
    "filename,change",
    [
        ("startup.json", {"phase": "phase-two"}),
        ("startup.json", {"task_profile": "standard"}),
        ("retained.json", {"status": "missing"}),
        ("retained.json", {"version_id": "other"}),
        ("outcome.json", {"status": "incomplete"}),
        ("outcome.json", {"acceptance": {}}),
        ("outcome.json", {"acceptance": {"E": {"status": "failed"}}}),
        ("outcome.json", {"input_manifest": {"acceptance_checks": []}}),
        ("outcome.json", {"input_manifest": {"acceptance_checks": ["A", "B", "C", "D"]}}),
        ("outcome.json", {"input_manifest": {}}),
    ],
)
def test_skills_source_must_have_real_host_e_success(tmp_path, filename, change):
    start = e_source(tmp_path)
    path = start.previous_run / filename
    path.write_text(json.dumps(dict(json.loads(path.read_text()), **change)))
    with pytest.raises(ValueError):
        launch.prepare_task_start(start, "phase-one", "skills-extension", "F goal")


def test_missing_host_outcome_rejects_copied_template_success(tmp_path):
    start = e_source(tmp_path)
    (start.previous_run / "outcome.json").unlink()
    with pytest.raises(ValueError):
        launch.prepare_task_start(start, "phase-one", "skills-extension", "F goal")


@pytest.mark.parametrize(
    "flags",
    [
        ["--resume-from"],
        ["--run-harness", "--phase", "phase-two"],
        ["--run-harness", "--check"],
        ["--run-harness", "--verify-deployment"],
    ],
)
def test_initial_feedback_invalid_skills_cli_before_deployment(tmp_path, monkeypatch, flags):
    start = e_source(tmp_path)
    args = []
    for flag in flags:
        args.append(flag)
        if flag in ("--resume-from", "--run-harness"):
            args.append(str(start.previous_run))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "launch",
            "--task-profile",
            "skills-extension",
            "--initial-feedback-file",
            "missing",
            *args,
        ],
    )
    monkeypatch.setattr(launch, "load_dotenv", lambda *a, **k: pytest.fail("deployment accessed"))
    with pytest.raises(SystemExit) as error:
        launch.main()
    assert error.value.code == 2


@pytest.mark.parametrize("feedback", [None, "Approved fresh F only"])
def test_valid_skills_cli_and_retained_snapshot_unchanged(tmp_path, monkeypatch, feedback):
    from a3_research_agent.host.runs.continuation import load_run_result, publish_run_result
    from a3_research_agent.host.sessions.versions import CandidateVersions

    start = e_source(tmp_path)
    source = start.previous_run
    workspace = source / "workspace"
    workspace.mkdir()
    for name, content in start.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "continuation.json").write_text(
        json.dumps({"command": start.command, "files": list(start.files)})
    )
    publish_run_result(source, workspace, CandidateVersions(source / "versions"), "main", None)
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    loaded = load_run_result(source)
    fresh, context = launch.prepare_task_start(loaded, "phase-one", "skills-extension", "F only")
    assert context["mode"] == "fresh"
    assert fresh.files["checkpoint.json"] != loaded.files["checkpoint.json"]
    args = [
        "launch",
        "--phase",
        "phase-one",
        "--task-profile",
        "skills-extension",
        "--run-harness",
        str(source),
    ]
    if feedback:
        note = tmp_path / "approved.md"
        note.write_text(feedback)
        args += ["--initial-feedback-file", str(note)]
    monkeypatch.setattr(sys, "argv", args)

    class DeploymentReached(Exception):
        pass

    def stop(*args, **kwargs):
        raise DeploymentReached

    monkeypatch.setattr(launch, "load_dotenv", stop)
    with pytest.raises(DeploymentReached):
        launch.main()
    assert before == {
        str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize("feedback", ["", " \n", "x" * 65537])
def test_skills_invalid_feedback_rejected(tmp_path, feedback):
    with pytest.raises(ValueError, match="feedback"):
        launch.prepare_task_start(
            e_source(tmp_path), "phase-one", "skills-extension", "F only", initial_feedback=feedback
        )


def test_e_resume_cannot_change_to_f(tmp_path, monkeypatch):
    start = e_source(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["launch", "--resume-from", str(start.previous_run), "--task-profile", "skills-extension"],
    )
    monkeypatch.setattr(launch, "load_dotenv", lambda *a, **k: pytest.fail("deployment accessed"))
    with pytest.raises(SystemExit) as error:
        launch.main()
    assert error.value.code == 2


def test_source_inherited_success_is_not_current_f_acceptance(tmp_path):
    from a3_research_agent.host.runs.supervisor import evaluate_outcome

    start = e_source(tmp_path)
    fresh, _ = launch.prepare_task_start(start, "phase-one", "skills-extension", "F only")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name, content in fresh.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    assert evaluate_outcome(workspace, ("F",))["status"] == "continue"
    (workspace / "run-outcome.json").write_text(start.files["run-outcome.json"])
    assert evaluate_outcome(workspace, ("F",))["status"] == "continue"


@pytest.mark.parametrize("feedback", [None, "New F notification"])
def test_fresh_skills_archives_old_e_feedback_and_preserves_old_archives(tmp_path, feedback):
    start = e_source(tmp_path)
    start.files["initial-feedback.md"] = "Old E notification"
    start.files["inherited-task-state/task.txt"] = "Old A-D task"
    fresh, context = launch.prepare_task_start(
        start, "phase-one", "skills-extension", "F only", initial_feedback=feedback
    )
    assert fresh.files["inherited-task-state/task.txt"] == "Old A-D task"
    assert (
        fresh.files["inherited-task-state/skills-extension/initial-feedback.md"]
        == "Old E notification"
    )
    assert (
        context["initialized_task_files"]["initial-feedback.md"]["archive_path"]
        == "inherited-task-state/skills-extension/initial-feedback.md"
    )
    if feedback:
        assert fresh.files["initial-feedback.md"] == feedback
    else:
        assert "initial-feedback.md" not in fresh.files
    assert "Old E notification" not in fresh.files["checkpoint.json"]
    assert start.files["initial-feedback.md"] == "Old E notification"


def test_skills_archive_collision_fails_closed(tmp_path):
    start = e_source(tmp_path)
    start.files["inherited-task-state/skills-extension/task.txt"] = "do not overwrite"
    with pytest.raises(ValueError, match="archive"):
        launch.prepare_task_start(start, "phase-one", "skills-extension", "F only")


def test_real_e_snapshot_shape_keeps_e_history_without_terminal_f(tmp_path):
    from a3_research_agent.host.runs.supervisor import evaluate_outcome

    start = e_source(tmp_path)
    del start.files["run-outcome.json"]
    start.files["initial-feedback.md"] = "Old E notification"
    start.files["web-extension-progress.md"] = "Historical E passed"
    fresh, context = launch.prepare_task_start(start, "phase-one", "skills-extension", "F only")
    assert "run-outcome.json" not in fresh.files
    assert "retired_terminal" not in context
    assert fresh.files["web-extension-progress.md"] == "Historical E passed"
    assert (
        fresh.files["inherited-task-state/skills-extension/initial-feedback.md"]
        == "Old E notification"
    )
    assert json.loads(fresh.files["checkpoint.json"]) == [{"role": "user", "content": "F only"}]
    workspace = tmp_path / "fresh-f"
    workspace.mkdir()
    for name, content in fresh.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    assert evaluate_outcome(workspace, ("F",))["status"] == "continue"


@pytest.mark.parametrize("manifest", ["resume.json", "continuation.json"])
def test_skills_rejects_dangling_explicit_feedback_manifest(tmp_path, manifest):
    start = e_source(tmp_path)
    start.files["initial-feedback.md"] = "Old E note"
    declaration = json.dumps(
        {"command": start.command, "files": ["agent.py", "checkpoint.json", "initial-feedback.md"]}
    )
    start.files[manifest] = declaration
    with pytest.raises(ValueError, match="initial-feedback.*manifest"):
        launch.prepare_task_start(start, "phase-one", "skills-extension", "F only")
    assert start.files[manifest] == declaration
    assert start.files["initial-feedback.md"] == "Old E note"
    fresh, _ = launch.prepare_task_start(
        start, "phase-one", "skills-extension", "F only", initial_feedback="New F note"
    )
    assert fresh.files[manifest] == declaration
    assert fresh.files["initial-feedback.md"] == "New F note"
    assert fresh.files["inherited-task-state/skills-extension/initial-feedback.md"] == "Old E note"
