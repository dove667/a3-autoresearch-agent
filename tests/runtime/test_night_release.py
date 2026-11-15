import json
from pathlib import Path

import pytest

from a3_research_agent.cli.night_release import (
    NightSchedule,
    night_state_directory,
    prepare_release,
    publish_release,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def release(tmp_path, monkeypatch):
    monkeypatch.setattr("a3_research_agent.cli.night_release.native_source", lambda *args: ROOT)
    anchor = tmp_path / "project/trajectories/run-original"
    (anchor / ".host").mkdir(parents=True)
    (anchor / ".host/state.json").write_text('{"status":"suspended"}')
    handoff = tmp_path / "handoff.md"
    handoff.write_text("## 已核对结论\n\n维护记录已保存。\n\n## 待办\n\n继续原研究状态。\n")
    return anchor, handoff


def test_release_freezes_current_prompts_and_requires_exact_digest(release):
    anchor, handoff = release
    assert (
        night_state_directory(anchor)
        == anchor.parent.parent / ".runtime/night-window" / anchor.name
    )
    from a3_research_agent.host.inputs.versions import InputVersions

    versions = InputVersions(ROOT, anchor)
    versions.activate(versions.prepare(), "start")
    proposal = prepare_release(ROOT, handoff, commit="a" * 40, run=anchor)
    manifest = json.loads(proposal["package"]["run.json"])
    assert manifest["version"] == 1
    assert "phase" not in manifest and "task_profile" not in manifest
    assert set(proposal["package"]) == {"run.json", ".agent/checkpoint.json"}
    with pytest.raises(ValueError, match="digest"):
        publish_release(anchor, proposal, "stale")
    publish_release(anchor, proposal, proposal["digest"])
    directory = night_state_directory(anchor) / "night-releases" / proposal["digest"]
    NightSchedule.verify(directory, proposal["digest"])
    (directory / "package/.agent/checkpoint.json").parent.mkdir(parents=True, exist_ok=True)
    (directory / "package/.agent/checkpoint.json").write_text("changed")
    with pytest.raises(ValueError, match="digest"):
        NightSchedule.verify(directory, proposal["digest"])


def test_published_git_source_excludes_live_checkout_and_unpublished_commits(tmp_path):
    import subprocess

    from a3_research_agent.host.published_harness import published_source

    def git(*args):
        return subprocess.check_output(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            text=True,
        ).strip()

    git("init", "--quiet")
    package = tmp_path / "src/a3_research_agent"
    (package / "harness").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    tokenizer = package / "token_count.py"
    tokenizer.write_text("MARKER = 'published'\n")
    agent = package / "harness/agent.py"
    agent.write_text("print('published')\n")
    git("add", "src")
    git("commit", "--quiet", "-m", "publish")
    commit = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", commit)
    agent.write_text("print('unpublished')\n")
    tokenizer.write_text("MARKER = 'unpublished'\n")
    git("commit", "--quiet", "-am", "local changes")
    unpublished = git("rev-parse", "HEAD")
    (tmp_path / ".env").write_text("PRIVATE=private")
    source = published_source(tmp_path, commit)
    assert source == tmp_path / ".runtime/published-harness/v2" / commit
    pinned_agent = source / "a3_research_agent/harness/agent.py"
    assert pinned_agent.read_text() == "print('published')\n"
    assert (source / "a3_research_agent/token_count.py").read_text() == "MARKER = 'published'\n"
    assert not (source / ".git").exists()
    assert not (source / ".env").exists()
    with pytest.raises(ValueError, match="origin/main"):
        published_source(tmp_path, unpublished)
    pinned_agent.write_text("tampered")
    with pytest.raises(ValueError, match="differs"):
        published_source(tmp_path, commit)
