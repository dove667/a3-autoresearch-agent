import json
from pathlib import Path

import pytest

from a3_research_agent.cli.night_release import NightSchedule, prepare_release, publish_release

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def release(tmp_path):
    anchor = tmp_path / "run-original"
    (anchor / ".host").mkdir(parents=True)
    (anchor / ".host/state.json").write_text('{"status":"suspended"}')
    package = tmp_path / "draft"
    package.mkdir()
    (package / "agent.py").write_text("print('new harness')\n")
    (package / "run.json").write_text(
        json.dumps({"command": ["python", "agent.py", "--resume"], "files": ["agent.py"]})
    )
    handoff = tmp_path / "handoff.md"
    handoff.write_text("## 已核对结论\n\n模型 A 有报告。\n\n## 待办\n\n核对作业 123。\n")
    return anchor, package, handoff


def test_confirmed_release_switches_once_and_reuses_target_after_restart(release):
    anchor, package, handoff = release
    schedule = NightSchedule(anchor, ["python", "launch", "--resume-from", str(anchor)])
    assert schedule.select()[0][-1] == str(anchor)
    proposal = prepare_release(ROOT, package, handoff)
    publish_release(anchor, proposal, proposal["digest"])
    (package / "agent.py").write_text("changed after publish")
    command, target, env = schedule.select()
    assert "--run-harness" in command and "--resume-from" not in command
    assert env["A3_NIGHT_WINDOW_TARGET"] == str(target)
    snapshot = Path(command[command.index("--run-harness") + 1])
    assert (snapshot / "agent.py").read_text() == "print('new harness')\n"
    assert json.loads((snapshot / "checkpoint.json").read_text()) == []
    assert {p.name for p in snapshot.iterdir()} == {"run.json", "agent.py", "checkpoint.json"}
    assert schedule.select()[1] == target
    (target / "startup.json").write_text("{}")
    (target / ".host").mkdir()
    (target / ".host/state.json").write_text('{"status":"suspended"}')
    assert schedule.select() == (["python", "launch", "--resume-from", str(target)], target, {})
    assert anchor.is_dir()


def test_publication_requires_exact_digest_and_rejects_old_context(release):
    anchor, package, handoff = release
    proposal = prepare_release(ROOT, package, handoff)
    with pytest.raises(ValueError, match="digest"):
        publish_release(anchor, proposal, "stale")
    (package / "memory.jsonl").write_text("{}")
    (package / "run.json").write_text(
        json.dumps({"command": ["python", "agent.py"], "files": ["agent.py", "memory.jsonl"]})
    )
    with pytest.raises(ValueError, match="Python source"):
        prepare_release(ROOT, package, handoff)


def test_release_produces_only_current_goal_and_reviewed_handoff(release):
    from a3_research_agent.host.inputs.task_preparation import prepare_custom_task
    from a3_research_agent.host.runs.continuation import resolve_harness_start

    anchor, package, handoff = release
    (package / "checkpoint.json").write_text('[{"role":"user","content":"old tool"}]')
    (package / "history.jsonl").write_text("old traces")
    proposal = prepare_release(ROOT, package, handoff)
    publish_release(anchor, proposal, proposal["digest"])
    schedule = NightSchedule(
        anchor, ["python", "launch", "--resume-from", str(anchor), "--feedback-file", "old.md"]
    )
    command, target, _ = schedule.select()
    snapshot = Path(command[command.index("--run-harness") + 1])
    start = resolve_harness_start(snapshot)
    fresh, _ = prepare_custom_task(start, "new goal", proposal["inputs"]["initial-feedback.md"])
    messages = json.loads(fresh.files["checkpoint.json"])
    assert len(messages) == 2
    assert messages[0]["content"] == "new goal"
    assert handoff.read_text().strip() in messages[1]["content"]
    assert "old tool" not in str(messages)
    assert "history.jsonl" not in fresh.files
    assert "--feedback-file" not in command

    (target / "startup.json").write_text("{}")
    (target / ".host").mkdir()
    (target / ".host/state.json").write_text('{"status":"suspended"}')
    assert "--feedback-file" not in schedule.select()[0]


def test_tampering_or_partial_start_cannot_launch_another_run(release):
    anchor, package, handoff = release
    proposal = prepare_release(ROOT, package, handoff)
    publish_release(anchor, proposal, proposal["digest"])
    schedule = NightSchedule(anchor, ["python", "launch", "--resume-from", str(anchor)])
    command, target, _ = schedule.select()
    snapshot = Path(command[command.index("--run-harness") + 1])
    (snapshot / "agent.py").write_text("changed published source")
    with pytest.raises(ValueError, match="digest"):
        schedule.select()
    (target / "partial-start").touch()
    with pytest.raises(RuntimeError, match="inspection"):
        schedule.select()
    assert len(list(anchor.parent.glob("run-*"))) == 2


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
    agent = package / "harness/agent.py"
    agent.write_text("print('published')\n")
    git("add", "src")
    git("commit", "--quiet", "-m", "publish")
    commit = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", commit)
    agent.write_text("print('unpublished')\n")
    git("commit", "--quiet", "-am", "local changes")
    unpublished = git("rev-parse", "HEAD")
    (tmp_path / ".env").write_text("PRIVATE=private")
    source = published_source(tmp_path, commit)
    pinned_agent = source / "a3_research_agent/harness/agent.py"
    assert pinned_agent.read_text() == "print('published')\n"
    assert not (source / ".git").exists()
    assert not (source / ".env").exists()
    with pytest.raises(ValueError, match="origin/main"):
        published_source(tmp_path, unpublished)
    pinned_agent.write_text("tampered")
    with pytest.raises(ValueError, match="differs"):
        published_source(tmp_path, commit)
