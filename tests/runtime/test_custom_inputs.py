"""Generic retained tasks have explicit complete inputs and fresh task state."""

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from a3_research_agent.host import bootstrap as launch
from a3_research_agent.host.inputs.versions import InputVersions, input_sources
from a3_research_agent.host.runs.continuation import HarnessStart

ROOT = Path(__file__).resolve().parents[2]


def bundle(tmp_path):
    directory = tmp_path / "custom"
    directory.mkdir()
    for name, source in input_sources("phase-one").items():
        (directory / name).write_bytes((ROOT / source).read_bytes())
    (directory / "acceptance.md").write_text("Version: 1.0.0\n\n## search_reuse. Evidence\n")
    (directory / "goal.md").write_text("Version: 1.0.0\n\nResearch a useful skill\n")
    (directory / "notes").mkdir()
    (directory / "notes/references.md").write_text("Project references\n")
    return directory


def retained():
    return HarnessStart(
        ["python", "agent.py", "--resume"],
        {
            "agent.py": "retained code",
            "skills/index.json": "catalog",
            "memory.jsonl": "reusable memory",
            "checkpoint.json": json.dumps([{"role": "user", "content": "old F"}] * 482),
            "task.txt": "old F",
            "run-outcome.json": '{"status":"complete"}',
            "progress.md": "all complete",
            "trajectory.jsonl": "old trajectory",
            "initial-feedback.md": "finish old F",
            "resume.json": '{"files":["initial-feedback.md"]}',
        },
        Path("/source/F-run"),
        "ready-F-version",
    )


def test_custom_bundle_snapshots_all_files_and_freezes_resume(tmp_path):
    directory = bundle(tmp_path)
    (directory / "initial-feedback.md").write_text("New approved task, not resume F")
    run = tmp_path / "run"
    versions = InputVersions(ROOT, run, custom_dir=directory)
    proposal = versions.prepare()
    manifest = versions.activate(proposal, "start")
    assert manifest["input_mode"] == "custom"
    assert manifest["acceptance_checks"] == ["search_reuse"]
    assert versions.acceptance_checks() == ("search_reuse",)
    for name in proposal["contents"]:
        assert (run / "inputs" / name).read_text() == proposal["contents"][name]
        assert (
            manifest["files"][name]["sha256"]
            == hashlib.sha256((directory / name).read_bytes()).hexdigest()
        )
    assert "Research a useful skill" in versions.model_instructions()[1]
    assert "Project references" not in versions.model_instructions()[1]  # Mounted, not injected.
    shutil.rmtree(directory)
    restored = InputVersions(ROOT, run)
    assert restored.prepare()["contents"] == proposal["contents"]
    assert restored.prepare()["digest"] == proposal["digest"]
    second = restored.activate(restored.prepare(), "resume")
    assert second["input_mode"] == "custom"
    assert second["files"] == manifest["files"]
    assert versions.prepare()["contents"] == proposal["contents"]


def test_custom_snapshot_tampering_rejected(tmp_path):
    directory = bundle(tmp_path)
    run = tmp_path / "run"
    versions = InputVersions(ROOT, run, custom_dir=directory)
    manifest = versions.activate(versions.prepare(), "start")
    (run / "input-revisions" / manifest["revision"] / "goal.md").write_text("tampered")
    with pytest.raises(ValueError):
        InputVersions(ROOT, run).prepare()


@pytest.mark.parametrize("feedback", [None, "Human starts a separate skill research task"])
def test_custom_task_does_not_inherit_terminal_or_messages(tmp_path, feedback):
    start = retained()
    before = dict(start.files)
    fresh, context = launch.prepare_task_start(
        start, "phase-one", "standard", "new goal", custom_inputs=True, initial_feedback=feedback
    )
    assert start.files == before
    assert json.loads(fresh.files["checkpoint.json"])[0] == {"role": "user", "content": "new goal"}
    assert len(json.loads(fresh.files["checkpoint.json"])) == (2 if feedback else 1)
    assert json.loads(fresh.files["run-outcome.json"])["status"] == "continue"
    assert fresh.files["task.txt"] == "new goal"
    assert fresh.files["initial-feedback.md"] == (feedback or "")
    assert fresh.files["trajectory.jsonl"] == ""
    for name in ("agent.py", "skills/index.json", "memory.jsonl", "resume.json"):
        assert fresh.files[name] == before[name]
    for name, item in context["initialized_task_files"].items():
        assert fresh.files[item["archive_path"]] == before[name]
    assert context["previous_message_count"] == 482
    assert context["inherited_acceptance"]["status"] == "historical-not-recertified"
    assert "checks" not in context["inherited_acceptance"]
    # Another task inherits the prior archive without collision.
    fresh.source_version = "next-version"
    next_task, _ = launch.prepare_custom_task(fresh, "next goal")
    assert len(next_task.files) > len(fresh.files)


@pytest.mark.parametrize("damage", ["missing", "symlink", "reserved", "binary"])
def test_bad_custom_input_rejected_before_deployment(tmp_path, monkeypatch, damage):
    directory = bundle(tmp_path)
    if damage == "missing":
        (directory / "SOUL.md").unlink()
    elif damage == "symlink":
        (directory / "link").symlink_to(directory / "goal.md")
    elif damage == "reserved":
        (directory / "deployment.json").write_text("{}")
    else:
        (directory / "notes/binary").write_bytes(b"\xff")
    monkeypatch.setattr(
        sys, "argv", ["launch", "--run-harness", "unused", "--agent-inputs-dir", str(directory)]
    )
    monkeypatch.setattr(launch, "load_dotenv", lambda *a, **k: pytest.fail("deployment accessed"))
    with pytest.raises(SystemExit) as error:
        launch.main()
    assert error.value.code == 2


@pytest.mark.parametrize(
    "flags",
    [
        ["--resume-from", "old"],
        ["--run-harness", "old", "--task-profile", "skills-extension"],
        ["--run-harness", "old", "--initial-feedback-file", "f"],
        ["--check"],
    ],
)
def test_custom_cli_conflicts_fail_early(tmp_path, monkeypatch, flags):
    monkeypatch.setattr(sys, "argv", ["launch", "--agent-inputs-dir", "missing", *flags])
    monkeypatch.setattr(launch, "load_dotenv", lambda *a, **k: pytest.fail("deployment accessed"))
    with pytest.raises(SystemExit) as error:
        launch.main()
    assert error.value.code == 2


def test_prevalidated_bytes_used_even_if_original_directory_disappears(tmp_path):
    directory = bundle(tmp_path)
    proposal = InputVersions(ROOT, tmp_path / "preview", custom_dir=directory).prepare()
    shutil.rmtree(directory)
    versions = InputVersions.from_proposal(ROOT, tmp_path / "new-run", proposal)
    manifest = versions.activate(proposal, "start")
    assert versions.contents == proposal["contents"]
    assert manifest["input_mode"] == "custom"
    assert versions.prepare()["digest"] == proposal["digest"]


@pytest.mark.parametrize(
    "acceptance", ["Version: 1.0.0\nNo checks", "Version: 1.0.0\n## same. One\n## same. Two"]
)
def test_custom_acceptance_ids_required_and_unique(tmp_path, acceptance):
    directory = bundle(tmp_path)
    (directory / "acceptance.md").write_text(acceptance)
    with pytest.raises(ValueError, match="unique"):
        InputVersions(ROOT, tmp_path / "run", custom_dir=directory).prepare()


def test_custom_from_real_ready_snapshot_validates_without_source_profile(tmp_path, monkeypatch):
    from a3_research_agent.host.runs.continuation import publish_run_result
    from a3_research_agent.host.runs.supervisor import evaluate_outcome
    from a3_research_agent.host.sessions.versions import CandidateVersions

    directory = bundle(tmp_path)
    (directory / "initial-feedback.md").write_text("New approved research task")
    source = tmp_path / "F-source"
    workspace = source / "workspace"
    workspace.mkdir(parents=True)
    start = retained()
    for name, content in start.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "continuation.json").write_text(
        json.dumps({"command": start.command, "files": list(start.files)})
    )
    publish_run_result(source, workspace, CandidateVersions(source / "versions"), "main", None)
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    monkeypatch.setattr(
        sys, "argv", ["launch", "--run-harness", str(source), "--agent-inputs-dir", str(directory)]
    )

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
    fresh, _ = launch.prepare_custom_task(launch.resolve_harness_start(source), "new task")
    for name, content in fresh.files.items():
        target = tmp_path / "new-workspace" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    assert evaluate_outcome(tmp_path / "new-workspace", ("search_reuse",))["status"] == "continue"
    record = json.loads((source / "retained.json").read_text())
    record["status"] = "missing"
    (source / "retained.json").write_text(json.dumps(record))
    with pytest.raises(SystemExit) as error:
        launch.main()
    assert error.value.code == 2


def test_input_names_cannot_collide_with_atomic_write_scratch(tmp_path):
    directory = bundle(tmp_path)
    names = ("input-manifest.json.tmp", "manifest.json.tmp", "goal.md.tmp")
    for name in names:
        (directory / name).write_text(f"Keep {name}")
    run = tmp_path / "run"
    versions = InputVersions(ROOT, run, custom_dir=directory)
    proposal = versions.prepare()
    manifest = versions.activate(proposal, "start")
    for name in names:
        assert (run / "inputs" / name).read_text() == f"Keep {name}"
        assert (run / "input-revisions" / manifest["revision"] / name).read_text() == f"Keep {name}"
    assert InputVersions(ROOT, run).prepare()["digest"] == proposal["digest"]


def test_legacy_acceptance_heading_forms_remain_recognized(tmp_path):
    root = tmp_path / "repo"
    for source in input_sources("phase-one").values():
        path = root / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / source).read_bytes())
    (root / input_sources("phase-one")["acceptance.md"]).write_text(
        "Version: 1.0.0\n## A.\n## B.\tTitle\n## C. Title\n"
    )
    versions = InputVersions(root, tmp_path / "run")
    manifest = versions.activate(versions.prepare(), "start")
    assert manifest["acceptance_checks"] == ["A", "B", "C"]
    assert versions.acceptance_checks() == ("A", "B", "C")
