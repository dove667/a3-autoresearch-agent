import subprocess

import pytest

from runtime.local.candidate_versions import CandidateVersions


def test_immutable_versions_and_worktrees_keep_parallel_edits_separate(tmp_path):
    versions = CandidateVersions(tmp_path)
    first = versions.create("parent", {"entry.py": "print('original')"})
    versions.grant("parent", "child", first["version_id"])
    left = versions.checkout("parent", first["version_id"], "left")
    right = versions.checkout("child", first["version_id"], "right")
    (left / "entry.py").write_text("print('edited')")
    assert (right / "entry.py").read_text() == "print('original')"
    assert not (left / ".git").exists()
    assert (left.parent / ".git").is_file()
    second = versions.create("child", {"entry.py": "print('next')"}, first["version_id"])
    assert second["parent_version"] == first["version_id"]
    assert second["commit"] != first["commit"]
    parents = subprocess.check_output(
        [
            "git",
            "--git-dir",
            str(versions.repository),
            "rev-list",
            "--parents",
            "-n",
            "1",
            second["commit"],
        ],
        text=True,
    ).split()
    assert parents[1] == first["commit"]
    fresh = CandidateVersions(tmp_path)
    replay = fresh.checkout("parent", first["version_id"], "replay")
    assert (replay / "entry.py").read_text() == "print('original')"


def test_versions_are_private_unless_explicitly_granted(tmp_path):
    versions = CandidateVersions(tmp_path)
    item = versions.create("a", {"entry.py": "pass"})
    assert versions.list("b") == []
    with pytest.raises(PermissionError):
        versions.checkout("b", item["version_id"], "b")
    with pytest.raises(PermissionError):
        versions.create("b", {"entry.py": "pass"}, item["version_id"])
    with pytest.raises(ValueError):
        versions.create("a", {"../outside": "bad"})
    assert not list((tmp_path / "worktrees").iterdir())


@pytest.mark.parametrize("name", [".git/config", "dir/.git/config", ".env", "nested/.env.local"])
def test_credentials_and_git_control_files_cannot_be_saved(tmp_path, name):
    versions = CandidateVersions(tmp_path)
    with pytest.raises(ValueError):
        versions.create("a", {name: "secret"})
    with pytest.raises(ValueError):
        versions.create_local("a", {name: "secret"})


@pytest.mark.parametrize(
    "files",
    [
        {f"file_{index}.txt": "x" for index in range(129)},
        {"trajectory.jsonl": "x" * (20 * 1024 * 1024 + 1)},
    ],
)
def test_agent_upload_limits_remain_in_force(tmp_path, files):
    versions = CandidateVersions(tmp_path)
    with pytest.raises(ValueError, match="Candidate input exceeds"):
        versions.create("agent", files)
    assert versions.list("agent") == []
    assert not list(versions.worktrees.iterdir())


def test_versioned_source_ignores_ignore_rules_and_preserves_original_bytes(tmp_path):
    versions = CandidateVersions(tmp_path)
    files = {
        ".gitignore": "*.py\n",
        ".gitattributes": "*.py text eol=lf\n",
        "entry.py": "print('原样')\r\n",
        "nested/next.py": "print('next')\r\n",
    }
    item = versions.create("a", files)
    source = versions.checkout("a", item["version_id"], "checkout")
    for name, content in files.items():
        assert (source / name).read_bytes() == content.encode()
