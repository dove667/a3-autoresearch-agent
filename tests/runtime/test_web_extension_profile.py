"""E-only retained tasks preserve provenance without replaying old messages."""

import json
import sys

import pytest

from runtime.local import input_versions, launch
from runtime.local.continuation import HarnessStart


def source_tree(tmp_path):
    root = tmp_path / "source"
    for name, relative in input_versions.input_sources("phase-one", "web-extension").items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "Version: 0.1.0\n" + ("## E. Web interface\n" if name == "acceptance.md" else name)
        if name == "bootstrap.yaml":
            text = "version: 0.1.0\nagent:\n  system_template: task\n  instance_template: task\nrun:\n  task: task\n"
        path.write_text(text)
    return root


def test_profile_inputs_acceptance_reload_and_archive(tmp_path):
    root = source_tree(tmp_path)
    run = tmp_path / "run"
    versions = input_versions.InputVersions(root, run, task_profile="web-extension")
    proposal = versions.prepare()
    assert proposal["task_profile"] == "web-extension"
    manifest = versions.activate(proposal, "start")
    assert manifest["acceptance_checks"] == ["E"]
    assert versions.acceptance_checks() == ("E",)
    assert "protocol.md" not in manifest["files"]
    assert manifest["files"]["goal.md"]["source"] == "evals/phase-one-web/goal.md"
    assert "web-extension" in versions.model_instructions()[1]
    restored = input_versions.InputVersions(root, run)
    assert restored.task_profile == "web-extension"
    restored.activate(restored.prepare(), "resume")
    with pytest.raises(ValueError, match="profile"):
        input_versions.InputVersions(root, run, task_profile="standard")
    wrong = dict(proposal, task_profile="standard")
    with pytest.raises(ValueError, match="profile"):
        versions.activate(wrong, "update")
    (root / "evals/phase-one-web/goal.md").write_text("Version: 0.1.0\nchanged")
    with pytest.raises(ValueError, match="version increment"):
        restored.prepare()


def test_legacy_profile_defaults_and_conflicts(tmp_path):
    (tmp_path / "startup.json").write_text(json.dumps({"phase": "phase-one"}))
    assert input_versions.resolve_task_profile(tmp_path) == "standard"
    with pytest.raises(ValueError, match="profile"):
        input_versions.resolve_task_profile(tmp_path, "web-extension")
    with pytest.raises(ValueError, match="phase-one"):
        input_versions.input_sources("phase-two", "web-extension")


def test_new_extension_checkpoint_and_provenance(tmp_path):
    original = json.dumps([{"role": "user", "content": "old task"}] * 935)
    start = HarnessStart(
        ["python", "agent.py"],
        {
            "checkpoint.json": original,
            "agent.py": "code",
            "memory/lesson": "keep",
            "evidence.json": "old evidence",
        },
        tmp_path,
        "v1",
    )
    fresh, context = launch.prepare_task_start(start, "phase-one", "web-extension", "E-only goal")
    assert json.loads(fresh.files["checkpoint.json"]) == [
        {"role": "user", "content": "E-only goal"}
    ]
    assert start.files["checkpoint.json"] == original
    assert fresh.files["agent.py"] == "code"
    assert fresh.files["memory/lesson"] == "keep"
    assert fresh.files["evidence.json"] == "old evidence"
    assert context["previous_message_count"] == 935
    assert context["inherited_acceptance"] == {
        "checks": ["A", "B", "C", "D"],
        "status": "historical-not-recertified",
        "source_run": str(tmp_path.resolve()),
        "source_version": "v1",
    }
    assert launch.prepare_task_start(start, "phase-one", "standard", "goal") == (start, None)
    assert launch.prepare_task_start(start, "phase-two", "standard", "goal")[1]["mode"] == "fresh"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--task-profile", "web-extension"],
        ["--task-profile", "web-extension", "--verify-deployment"],
        ["--task-profile", "web-extension", "--phase", "phase-two", "--run-harness", "missing"],
        ["--task-profile", "web-extension", "--run-harness", "missing"],
        ["--task-profile", "web-extension", "--resume-from", "missing"],
    ],
)
def test_invalid_profile_rejected_before_deployment(monkeypatch, arguments):
    monkeypatch.setattr(sys, "argv", ["launch", *arguments])
    monkeypatch.setattr(launch, "load_dotenv", lambda *a, **k: pytest.fail("dotenv accessed"))
    with pytest.raises(SystemExit) as error:
        launch.main()
    assert error.value.code == 2


def test_extension_rejects_phase_two_source(tmp_path):
    (tmp_path / "startup.json").write_text(json.dumps({"phase": "phase-two"}))
    start = HarnessStart(["python", "agent.py"], {"checkpoint.json": "[]"}, tmp_path, "v1")
    with pytest.raises(ValueError, match="phase-one"):
        launch.prepare_task_start(start, "phase-one", "web-extension", "goal")


def test_existing_retained_source_is_immutable(tmp_path):
    from runtime.local.candidate_versions import CandidateVersions
    from runtime.local.continuation import load_run_result, publish_run_result

    source = tmp_path / "source-run"
    workspace = source / "workspace"
    workspace.mkdir(parents=True)
    files = {
        "agent.py": "unchanged",
        "checkpoint.json": json.dumps([{"role": "user", "content": "old"}] * 935),
        "memory/lesson": "keep",
        "evidence.json": "historical",
        "task.txt": "old A-D task",
        "trajectory.jsonl": "old events\n",
        "progress.md": "old completed",
        "run-outcome.json": '{"status":"complete"}',
    }
    for name, content in files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "continuation.json").write_text(
        json.dumps({"command": ["python", "agent.py"], "files": list(files)})
    )
    publish_run_result(source, workspace, CandidateVersions(source / "versions"), "main", None)
    for name in ("self-stop.json", "outcome.json"):
        (source / name).write_text('{"original":true}')
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    fresh, context = launch.prepare_task_start(
        load_run_result(source), "phase-one", "web-extension", "E-only"
    )
    assert context["mode"] == "fresh"
    assert fresh.files["checkpoint.json"] != files["checkpoint.json"]
    assert load_run_result(source).files == files
    assert before == {
        str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize("initial_note", [None, "Explicit approved E-only note\n"])
def test_valid_profile_cli_resolves_before_deployment(tmp_path, monkeypatch, initial_note):
    from runtime.local.candidate_versions import CandidateVersions
    from runtime.local.continuation import publish_run_result

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "checkpoint.json").write_text("[]")
    (workspace / "continuation.json").write_text(
        json.dumps({"command": ["python", "agent.py"], "files": ["checkpoint.json"]})
    )
    publish_run_result(tmp_path, workspace, CandidateVersions(tmp_path / "versions"), "main", None)
    monkeypatch.setattr(
        sys, "argv", ["launch", "--task-profile", "web-extension", "--run-harness", str(tmp_path)]
    )

    if initial_note is not None:
        note_path = tmp_path / "approved.md"
        note_path.write_text(initial_note)
        monkeypatch.setattr(sys, "argv", [*sys.argv, "--initial-feedback-file", str(note_path)])

    class DeploymentReached(Exception):
        pass

    def stop(*args, **kwargs):
        raise DeploymentReached

    monkeypatch.setattr(launch, "load_dotenv", stop)
    with pytest.raises(DeploymentReached):
        launch.main()


def test_profile_resume_mismatch_rejected_before_deployment(tmp_path, monkeypatch):
    (tmp_path / "startup.json").write_text(json.dumps({"task_profile": "web-extension"}))
    monkeypatch.setattr(
        sys, "argv", ["launch", "--resume-from", str(tmp_path), "--task-profile", "standard"]
    )
    monkeypatch.setattr(launch, "load_dotenv", lambda *a, **k: pytest.fail("dotenv accessed"))
    with pytest.raises(SystemExit) as error:
        launch.main()
    assert error.value.code == 2


@pytest.mark.parametrize("status", ["complete", "blocked"])
def test_fresh_extension_retires_copied_terminal_outcome(tmp_path, status):
    import hashlib

    from runtime.local.run_supervisor import evaluate_outcome

    old = json.dumps(
        {
            "status": status,
            "acceptance": {"E": {"status": "passed", "evidence": ["evidence.json"]}},
            "external_blocker": {
                "kind": "permission",
                "requires_human": True,
                "reason": "old",
                "required_action": "old",
                "evidence": ["evidence.json"],
            },
        }
    )
    start = HarnessStart(
        ["python", "agent.py"],
        {
            "checkpoint.json": "[]",
            "run-outcome.json": old,
            "evidence.json": "old evidence",
            "agent.py": "retained code",
            "continuation.json": json.dumps(
                {"command": ["python", "agent.py"], "files": ["agent.py", "checkpoint.json"]}
            ),
        },
        tmp_path / "source",
        "v1",
    )
    fresh, context = launch.prepare_task_start(start, "phase-one", "web-extension", "new E")
    assert start.files["run-outcome.json"] == old
    assert fresh.files["evidence.json"] == "old evidence"
    assert context["retired_terminal"] == {
        "file": "run-outcome.json",
        "sha256": hashlib.sha256(old.encode()).hexdigest(),
        "source_run": str((tmp_path / "source").resolve()),
        "source_version": "v1",
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name, content in fresh.files.items():
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_text(content)
    assert evaluate_outcome(workspace, ("E",))["status"] == "continue"
    (workspace / "run-outcome.json").write_text(old)
    assert evaluate_outcome(workspace, ("E",))["status"] == status


def test_extension_archives_only_task_state_and_initializes_current_files(tmp_path):
    import hashlib

    original = {
        "checkpoint.json": '[{"role":"user","content":"old AD"}]',
        "task.txt": "old full A-D task and old trial ID",
        "trajectory.jsonl": '{"old":"model events"}\n',
        "progress.md": "old completed",
        "run-outcome.json": '{"status":"complete"}',
        "agent.py": "unchanged code",
        "memory/lessons.jsonl": "unchanged memory",
        "acceptance-summary.json": "old AD evidence",
        "input-revalidation.json": "old evidence",
        "source-event.json": "unchanged source event",
    }
    start = HarnessStart(["python", "agent.py", "--resume"], dict(original), tmp_path, "v1")
    fresh, context = launch.prepare_task_start(start, "phase-one", "web-extension", "new E goal")
    assert start.files == original
    assert fresh.command == start.command
    assert fresh.files["task.txt"] == "new E goal"
    assert fresh.files["trajectory.jsonl"] == ""
    assert "web-extension" in fresh.files["progress.md"]
    assert json.loads(fresh.files["run-outcome.json"])["status"] == "continue"
    for name in ("task.txt", "trajectory.jsonl", "progress.md", "run-outcome.json"):
        archived = "inherited-task-state/" + name
        assert fresh.files[archived] == original[name]
        assert context["initialized_task_files"][name] == {
            "source_sha256": hashlib.sha256(original[name].encode()).hexdigest(),
            "archive_path": archived,
            "source_run": str(tmp_path.resolve()),
            "source_version": "v1",
        }
    for name in (
        "agent.py",
        "memory/lessons.jsonl",
        "acceptance-summary.json",
        "input-revalidation.json",
        "source-event.json",
    ):
        assert fresh.files[name] == original[name]
    assert "continuation.json" not in fresh.files
    standard, info = launch.prepare_task_start(start, "phase-two", "standard", "new task")
    for name in original.keys() - {"checkpoint.json"}:
        assert standard.files[name] == original[name]
    assert "initialized_task_files" not in info


@pytest.mark.parametrize(
    "collision",
    [
        "inherited-task-state/task.txt",
        "inherited-task-state",
        "inherited-task-state/task.txt/child",
    ],
)
def test_extension_archive_collision_fails_closed(tmp_path, collision):
    original = {"checkpoint.json": "[]", "task.txt": "old task", collision: "do not overwrite"}
    start = HarnessStart(["python", "agent.py"], dict(original), tmp_path, "v1")
    with pytest.raises(ValueError, match="archive"):
        launch.prepare_task_start(start, "phase-one", "web-extension", "new E")
    assert start.files == original


def test_initial_feedback_is_separate_user_message_and_preserves_source(tmp_path):
    import hashlib

    note = "只补 E。\nKeep credentials on Host.\n"
    source = tmp_path / "approved-note.md"
    original = {"checkpoint.json": '[{"role":"user","content":"old"}]', "agent.py": "code"}
    start = HarnessStart(["python", "agent.py"], dict(original), tmp_path, "v1")
    fresh, context = launch.prepare_task_start(
        start,
        "phase-one",
        "web-extension",
        "E goal",
        initial_feedback=note,
        initial_feedback_source=source,
    )
    sha = hashlib.sha256(note.encode()).hexdigest()
    messages = json.loads(fresh.files["checkpoint.json"])
    assert messages == [
        {"role": "user", "content": "E goal"},
        {"role": "user", "content": f"Initial human feedback (sha256: {sha})\n\n{note}"},
    ]
    assert fresh.files["initial-feedback.md"] == note
    assert context["initial_message_count"] == 2
    assert context["initial_feedback"] == {
        "sha256": sha,
        "source": str(source.resolve()),
        "bytes": len(note.encode()),
        "file": "initial-feedback.md",
        "mode": "initial-user-input",
    }
    assert start.files == original
    plain, plain_context = launch.prepare_task_start(start, "phase-one", "web-extension", "E goal")
    assert plain_context["initial_message_count"] == 1
    assert "initial-feedback.md" not in plain.files


@pytest.mark.parametrize("note", ["", "  \n", "x" * 65537])
def test_initial_feedback_invalid_content_rejected(tmp_path, note):
    start = HarnessStart(["python", "agent.py"], {"checkpoint.json": "[]"}, tmp_path, "v1")
    with pytest.raises(ValueError, match="feedback"):
        launch.prepare_task_start(
            start, "phase-one", "web-extension", "goal", initial_feedback=note
        )


@pytest.mark.parametrize("collision", ["initial-feedback.md", "initial-feedback.md/child"])
def test_initial_feedback_collision_rejected(tmp_path, collision):
    start = HarnessStart(
        ["python", "agent.py"], {"checkpoint.json": "[]", collision: "old"}, tmp_path, "v1"
    )
    with pytest.raises(ValueError, match="feedback"):
        launch.prepare_task_start(
            start, "phase-one", "web-extension", "goal", initial_feedback="new note"
        )
    assert start.files[collision] == "old"


@pytest.mark.parametrize(
    "flags",
    [
        [],
        ["--run-harness", "missing"],
        ["--resume-from", "missing", "--task-profile", "web-extension"],
        ["--run-harness", "missing", "--task-profile", "web-extension", "--check"],
        ["--verify-deployment"],
        ["--phase", "phase-two", "--run-harness", "missing"],
    ],
)
def test_initial_feedback_invalid_cli_before_deployment(monkeypatch, flags):
    monkeypatch.setattr(sys, "argv", ["launch", "--initial-feedback-file", "missing-note", *flags])
    monkeypatch.setattr(launch, "load_dotenv", lambda *a, **k: pytest.fail("dotenv accessed"))
    with pytest.raises(SystemExit) as error:
        launch.main()
    assert error.value.code == 2


@pytest.mark.parametrize("content", [b"", b" \n", b"\xff", b"x" * 65537])
def test_initial_feedback_cli_invalid_file_before_deployment(tmp_path, monkeypatch, content):
    (tmp_path / "retained.json").write_text("{}")
    note = tmp_path / "note.md"
    note.write_bytes(content)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "launch",
            "--run-harness",
            str(tmp_path),
            "--task-profile",
            "web-extension",
            "--initial-feedback-file",
            str(note),
        ],
    )
    monkeypatch.setattr(launch, "load_dotenv", lambda *a, **k: pytest.fail("dotenv accessed"))
    with pytest.raises(SystemExit) as error:
        launch.main()
    assert error.value.code == 2


@pytest.mark.parametrize("phase,profile", [("phase-one", "standard"), ("phase-two", "standard")])
def test_initial_feedback_cannot_affect_standard_tasks(tmp_path, phase, profile):
    start = HarnessStart(["python", "agent.py"], {"checkpoint.json": "[]"}, tmp_path, "v1")
    with pytest.raises(ValueError, match="feedback"):
        launch.prepare_task_start(start, phase, profile, "goal", initial_feedback="note")
