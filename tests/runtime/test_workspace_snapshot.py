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
    snapshot_checkout(source, candidate)
    assert (candidate / "module.py").read_text() == "VERSION = 2\n"
    assert (candidate / "new.py").is_file()
    assert not (candidate / ".env").exists()
    assert not (candidate / "private").exists()
    (candidate / "module.py").write_text("VERSION = 3\n")
    assert (source / "module.py").read_text() == "VERSION = 2\n"


def test_snapshot_preserves_deleted_tracked_files(tmp_path):
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    make_repository(source)
    (source / "module.py").unlink()
    snapshot_checkout(source, candidate)
    assert not (candidate / "module.py").exists()


def test_successor_keeps_frozen_goals_and_links_previous_blocker(tmp_path):
    previous, inputs = tmp_path / "previous", tmp_path / "next/inputs"
    (previous / "inputs").mkdir(parents=True)
    (previous / "workspace/run").mkdir(parents=True)
    digests = {}
    for name in ("goal.md", "acceptance.md"):
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
