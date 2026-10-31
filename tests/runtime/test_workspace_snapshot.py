import hashlib
import json
import subprocess

import pytest

from runtime.local.launch import freeze_inputs, snapshot_checkout


def make_repository(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / ".gitignore").write_text(".env\nprivate/\n")
    (path / "module.py").write_text("VERSION = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Baseline",
        ],
        check=True,
    )


def test_snapshot_keeps_edits_but_excludes_host_secrets(tmp_path):
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    make_repository(source)
    (source / ".env").write_text("PRIVATE_CREDENTIAL")
    (source / "private").mkdir()
    (source / "private/key").write_text("PRIVATE_KEY")
    (source / "module.py").write_text("VERSION = 2\n")
    (source / "new.py").write_text("NEW = True\n")
    source_head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"])
    source_status = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"])
    snapshot_checkout(source, candidate)
    assert (candidate / "module.py").read_text() == "VERSION = 2\n"
    assert (candidate / "new.py").is_file()
    assert not (candidate / ".env").exists()
    assert not (candidate / "private").exists()
    assert subprocess.check_output(["git", "-C", str(candidate), "status", "--porcelain"]) == b""
    assert subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"]) == source_head
    assert (
        subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"])
        == source_status
    )
    (candidate / "module.py").write_text("VERSION = 3\n")
    assert (source / "module.py").read_text() == "VERSION = 2\n"


def test_snapshot_excludes_tracked_and_untracked_report_projects(tmp_path):
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    make_repository(source)
    reports = source / "reports"
    reports.mkdir()
    (reports / "tracked.html").write_text("Presentation source")
    subprocess.run(["git", "-C", str(source), "add", "reports"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Report source",
        ],
        check=True,
    )
    (reports / "draft.html").write_text("Untracked presentation draft")

    snapshot_checkout(source, candidate)

    assert not (candidate / "reports").exists()
    assert (candidate / "module.py").is_file()
    assert subprocess.check_output(["git", "-C", str(candidate), "status", "--porcelain"]) == b""


def test_snapshot_preserves_deleted_tracked_files(tmp_path):
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    make_repository(source)
    (source / "module.py").unlink()
    snapshot_checkout(source, candidate)
    assert not (candidate / "module.py").exists()
    assert subprocess.check_output(["git", "-C", str(candidate), "status", "--porcelain"]) == b""


def test_successor_keeps_frozen_goals_and_links_previous_blocker(tmp_path):
    previous, inputs = tmp_path / "previous", tmp_path / "next/inputs"
    (previous / "inputs").mkdir(parents=True)
    (previous / "workspace/run").mkdir(parents=True)
    digests = {}
    for name in ("goal.md", "acceptance.md", "bootstrap.yaml", "execution.md"):
        content = f"Frozen {name}"
        (previous / "inputs" / name).write_text(content)
        digests[name] = hashlib.sha256(content.encode()).hexdigest()
    (previous / "startup.json").write_text(json.dumps({"inputs": digests}))
    (previous / "workspace/run/blocker.md").write_text("Missing deployment interface")
    inputs.mkdir(parents=True)
    freeze_inputs(inputs, previous)
    assert (inputs / "goal.md").read_text() == "Frozen goal.md"
    assert (inputs / "previous-blocker.md").read_text() == "Missing deployment interface"
    assert json.loads((inputs / "parent-run.json").read_text())["run_id"] == "previous"
    (previous / "inputs/acceptance.md").write_text("Changed acceptance")
    with pytest.raises(ValueError, match="Frozen"):
        freeze_inputs(inputs, previous)


def test_recovery_preserves_generated_code_and_evidence_without_old_auth(tmp_path):
    from runtime.local.recovery import stage_recovery

    previous = tmp_path / "previous"
    (previous / "workspace/run").mkdir(parents=True)
    (previous / "workspace/run/reflection.md").write_text("Measured failure and hypothesis")
    candidate = previous / "candidates/child/workspace"
    candidate.mkdir(parents=True)
    (candidate / "agent.py").write_text("VERSION = 2\n")
    (candidate / "result.json").write_text('{"score": 0, "api_key": "old-secret-token"}')
    (candidate / "partial.json").write_text('{"message": "old-secret-token"')
    (candidate / ".env").write_text("PRIVATE_AUTH")
    (candidate / "link").symlink_to(tmp_path)
    (previous / "trajectory.json").write_text(
        json.dumps(
            {
                "info": {
                    "exit_status": "InternalServerError",
                    "config": {"environment": {"env": {"A3_GATEWAY_TOKEN": "old-secret-token"}}},
                },
                "messages": [
                    {"role": "system", "content": "Use bash"},
                    {
                        "object": "response",
                        "output": [
                            {"type": "reasoning", "encrypted_content": "ENCRYPTED"},
                            {
                                "type": "function_call",
                                "name": "bash",
                                "arguments": '{"command":"echo READY"}',
                            },
                        ],
                    },
                ],
            }
        )
    )
    history = tmp_path / "recovery"
    index = stage_recovery(previous, history)
    assert index["parent_run"] == "previous"
    assert (history / "candidates/child/agent.py").read_text() == "VERSION = 2\n"
    assert (history / "bootstrap/reflection.md").read_text() == "Measured failure and hypothesis"
    assert not (history / "candidates/child/.env").exists()
    assert not (history / "candidates/child/link").exists()
    all_text = "".join(p.read_text() for p in history.rglob("*") if p.is_file())
    assert "old-secret-token" not in all_text
    assert "ENCRYPTED" not in all_text
    assert "READY" in all_text
    (candidate / "agent.py").write_text("VERSION = 3\n")
    assert (history / "candidates/child/agent.py").read_text() == "VERSION = 2\n"


def test_recovery_survives_partial_root_and_child_trajectories(tmp_path):
    from runtime.local.recovery import stage_recovery

    previous = tmp_path / "previous"
    (previous / "workspace/run").mkdir(parents=True)
    (previous / "trajectory.json").write_text('{"messages": [')
    (previous / "workspace/run/notes.md").write_text("Saved progress")
    candidate = previous / "candidates/child/workspace"
    candidate.mkdir(parents=True)
    (candidate / "agent.py").write_text("VERSION = 2\n")
    (candidate / "trajectory.jsonl").write_text(
        json.dumps(
            {
                "api_key": "old-child-secret",
                "output": [{"type": "reasoning", "encrypted_content": "ENCRYPTED"}],
                "event": "model",
            }
        )
        + '\n{"unfinished":'
    )
    history = tmp_path / "recovery"
    index = stage_recovery(previous, history)
    assert index["warnings"]
    assert (history / "candidates/child/agent.py").read_text() == "VERSION = 2\n"
    exported = (history / "candidates/child/trajectory.jsonl").read_text()
    assert "old-child-secret" not in exported
    assert "ENCRYPTED" not in exported
    assert "model" in exported


def test_recovery_sanitizes_model_logs_inside_base64_artifacts(tmp_path):
    import base64

    from runtime.local.recovery import stage_recovery

    previous = tmp_path / "previous"
    source = previous / "workspace/run"
    source.mkdir(parents=True)
    nested = {
        "api_key": "nested-secret-token",
        "output": [
            {"type": "reasoning", "encrypted_content": "PRIVATE_REASONING"},
            {"type": "function_call", "name": "research", "arguments": "{}"},
        ],
        "usage": {"attribution": "PRIVATE_ATTRIBUTION"},
    }
    (source / "artifact.json").write_text(
        json.dumps(
            {
                "body": {
                    "encoding": "base64",
                    "data": base64.b64encode(json.dumps(nested).encode()).decode(),
                },
            }
        )
    )
    history = tmp_path / "history"
    stage_recovery(previous, history)
    result = json.loads((history / "bootstrap/artifact.json").read_text())["body"]
    decoded = base64.b64decode(result["data"]).decode()
    assert "nested-secret-token" not in decoded
    assert "PRIVATE_REASONING" not in decoded
    assert "PRIVATE_ATTRIBUTION" not in decoded
    assert "function_call" in decoded


def test_recovery_preserves_ancestor_binary_evidence_across_private_inputs(tmp_path):
    from runtime.local.recovery import stage_recovery

    first = tmp_path / "first"
    old_data = first / "workspace/run/memory.db"
    old_data.parent.mkdir(parents=True)
    old_data.write_bytes(b"SQLite format 3\x00\xff\x01")
    second = tmp_path / "second"
    stage_recovery(first, second / "private-inputs/history")
    third = tmp_path / "third"
    index = stage_recovery(second, third / "private-inputs/history")
    recovered = third / "private-inputs/history/earlier/bootstrap/memory.db"
    assert recovered.read_bytes() == old_data.read_bytes()
    assert any(row["path"] == "earlier/bootstrap/memory.db" for row in index["files"])
