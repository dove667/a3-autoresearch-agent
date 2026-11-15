"""Retained native results freeze selected research knowledge and published code."""

import json

import pytest
from native_run_support import COMMIT, native_run

from a3_research_agent.host.published_harness import COMMAND
from a3_research_agent.host.runs.continuation import (
    load_run_result,
    publish_run_result,
    resolve_harness_start,
)
from a3_research_agent.host.sessions.packages import load_published_manifest


def test_published_result_keeps_immutable_notes_and_research_without_old_context(tmp_path):
    run, workspace, _, _, _ = native_run(tmp_path)
    (workspace / "skills/local").mkdir(parents=True)
    (workspace / "skills/local/SKILL.md").write_text("Reusable experiment method")
    manifest = json.loads((run / "conversation/continuation.json").read_text())
    manifest["files"].append("skills/local/SKILL.md")
    for name in ("research-plan.json", "operational-state.json", "history.jsonl"):
        (workspace / name).write_text('{"old_task":"retained in the source run"}')
        manifest["files"].append(name)
    (run / "conversation/continuation.json").write_text(json.dumps(manifest))
    result = publish_run_result(run, workspace, "main")
    assert result["status"] == "ready" and result["harness_commit"] == COMMIT
    before = (workspace / "experiments/lesson.md").read_text()
    (workspace / "experiments/lesson.md").write_text("later edit")
    loaded = load_run_result(run)
    assert loaded.command == COMMAND and loaded.source_version == COMMIT
    assert loaded.files["experiments/lesson.md"] == before
    assert "skills/local/SKILL.md" not in loaded.files
    assert loaded.files["scripts/train.py"] == 'print("research source")\n'
    assert loaded.files["checkpoint.json"] == "[]"
    assert "trajectory.jsonl" not in loaded.files
    assert (
        not {"research-plan.json", "operational-state.json", "history.jsonl"} & loaded.files.keys()
    )
    assert loaded.previous_run == run


def test_modified_knowledge_fails_without_overwriting_its_evidence(tmp_path):
    run, workspace, _, _, _ = native_run(tmp_path)
    result = publish_run_result(run, workspace, "main")
    file = run / result["knowledge"]["directory"] / "experiments/lesson.md"
    file.write_text("corrupt")
    with pytest.raises(ValueError, match="knowledge content changed"):
        load_run_result(run)
    assert file.read_text() == "corrupt"


def test_unselected_and_missing_results_are_not_guessed(tmp_path):
    run, workspace, _, _, _ = native_run(tmp_path)
    (run / "conversation/continuation.json").unlink()
    assert publish_run_result(run, workspace, "main")["status"] == "missing"
    with pytest.raises(ValueError, match="retained result"):
        load_run_result(run)


@pytest.mark.parametrize("mutation", ["version", "command", "escape", "generated"])
def test_only_current_published_seed_manifest_is_accepted(tmp_path, mutation):
    directory = tmp_path / "seed"
    directory.mkdir()
    manifest = {
        "version": 1,
        "harness_commit": COMMIT,
        "command": COMMAND,
        "files": ["checkpoint.json"],
    }
    (directory / "checkpoint.json").parent.mkdir(parents=True, exist_ok=True)
    (directory / "checkpoint.json").write_text("[]")
    if mutation == "version":
        manifest.pop("version")
    elif mutation == "command":
        manifest["command"] = ["python", "agent.py"]
    elif mutation == "escape":
        manifest["files"] = ["../private"]
    elif mutation == "generated":
        manifest["files"].append("agent.py")
    (directory / "run.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_published_manifest(directory)


def test_published_seed_resolves_fixed_command_and_empty_context(tmp_path):
    (tmp_path / "checkpoint.json").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "checkpoint.json").write_text("[]\n")
    (tmp_path / "run.json").write_text(
        json.dumps(
            {
                "version": 1,
                "harness_commit": COMMIT,
                "command": COMMAND,
                "files": ["checkpoint.json"],
            }
        )
    )
    start = resolve_harness_start(tmp_path)
    assert start.harness_commit == COMMIT and start.files == {"checkpoint.json": "[]"}


@pytest.mark.parametrize("raw", [b"\x89PNG\r\n\x1a\n", b"\x00\x00binary"])
def test_retention_records_binary_results_in_shared_workspace_without_copying(tmp_path, raw):
    import hashlib

    run, workspace, _, _, _ = native_run(tmp_path)
    binary = workspace / "experiments/frame.png"
    binary.write_bytes(raw)
    declaration = run / "conversation/continuation.json"
    manifest = json.loads(declaration.read_text())
    manifest["files"].append("experiments/frame.png")
    declaration.write_text(json.dumps(manifest))

    result = publish_run_result(run, workspace, "main")
    assert result["knowledge"]["workspace_binary_files"] == [
        {
            "path": "experiments/frame.png",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "location": "shared_workspace",
        }
    ]
    loaded = load_run_result(run)
    assert "experiments/frame.png" not in loaded.files
    assert "experiments/lesson.md" in loaded.files
    assert "scripts/train.py" in loaded.files
    assert not (run / result["knowledge"]["directory"] / "experiments/frame.png").exists()
    assert binary.read_bytes() == raw


def test_binary_script_fails_retention_instead_of_being_silently_excluded(tmp_path):
    run, workspace, _, _, _ = native_run(tmp_path)
    (workspace / "scripts/train.py").write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        publish_run_result(run, workspace, "main")
    assert not (run / "retained.json").exists()
