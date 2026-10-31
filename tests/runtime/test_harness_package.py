import hashlib
import json

import pytest

from runtime.local.harness_package import load_harness_package
from runtime.local.recovery import stage_recovery


def test_package_loads_only_explicitly_selected_files(tmp_path):
    (tmp_path / "entry.py").write_text("print('retained')\n")
    (tmp_path / "handoff.md").write_text("Read the current frozen task.")
    (tmp_path / ".env").write_text("PRIVATE_AUTH")
    (tmp_path / "run.json").write_text(
        json.dumps({"command": ["python", "entry.py"], "files": ["entry.py", "handoff.md"]})
    )
    command, files = load_harness_package(tmp_path)
    assert command == ["python", "entry.py"]
    assert files == {
        "entry.py": "print('retained')\n",
        "handoff.md": "Read the current frozen task.",
    }


def test_package_manifest_rejects_files_outside_its_directory(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({"command": ["true"], "files": ["../private"]}))
    with pytest.raises(ValueError):
        load_harness_package(tmp_path)


def test_recovery_follows_only_host_managed_candidate_workspace_link(tmp_path):
    parent = tmp_path / "parent"
    workspace = parent / "versions/worktrees/candidate-a/source"
    workspace.mkdir(parents=True)
    (workspace / "memory.json").write_text('{"lesson": "keep source references"}')
    candidate = parent / "candidates/candidate-a"
    candidate.mkdir(parents=True)
    (candidate / "workspace").symlink_to(workspace, target_is_directory=True)
    unrelated = parent / "candidates/candidate-b"
    unrelated.mkdir()
    (unrelated / "workspace").symlink_to(tmp_path, target_is_directory=True)
    index = stage_recovery(parent, tmp_path / "recovery")
    assert [file["path"] for file in index["files"]] == ["candidates/candidate-a/memory.json"]


def test_retained_run_resumes_via_current_bootstrap_and_exports_prior_candidates(tmp_path):
    from runtime.local.launch import ROOT, bootstrap_source

    retained = tmp_path / "retained"
    retained.mkdir()
    assert bootstrap_source(retained) == ROOT
    assert bootstrap_source(None) == ROOT
    previous = retained / "workspace/harness"
    (previous / ".git").mkdir(parents=True)
    assert bootstrap_source(retained) == previous


def test_manifest_loading_preserves_line_endings(tmp_path):
    content = b"print('keep CRLF')\r\n"
    (tmp_path / "entry.py").write_bytes(content)
    (tmp_path / "run.json").write_text(
        json.dumps({"command": ["python", "entry.py"], "files": ["entry.py"]})
    )
    _, files = load_harness_package(tmp_path)
    assert files["entry.py"].encode() == content


def test_external_host_review_is_not_exported_to_agent(tmp_path):
    parent = tmp_path / "parent"
    (parent / "inputs").mkdir(parents=True)
    digests = {}
    for name in ("goal.md", "acceptance.md"):
        (parent / "inputs" / name).write_text("frozen")
        digests[name] = hashlib.sha256(b"frozen").hexdigest()
    (parent / "host-review.json").write_text(json.dumps({"inputs": digests}))
    destination = tmp_path / "recovery"
    index = stage_recovery(parent, destination)
    assert not (destination / "host-review.json").exists()
    assert not any(row["path"] == "host-review.json" for row in index["files"])
