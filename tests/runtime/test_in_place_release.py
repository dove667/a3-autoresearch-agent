"""Published maintenance releases retain research identity and working state."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from a3_research_agent.cli.night_release import (
    NightSchedule,
    night_state_directory,
    prepare_release,
    publish_release,
)
from a3_research_agent.host.inputs.versions import InputVersions
from a3_research_agent.host.published_harness import pin_run, research_workspace, run_commit
from a3_research_agent.host.runs.resume import load_resume_state
from a3_research_agent.host.runs.state import RunLease, RunStateStore

ROOT = Path(__file__).resolve().parents[2]


def fixture_file(run, workspace, name):
    root = (
        run / "conversation"
        if name
        in {
            "checkpoint.json",
            "pending-calls.json",
            "operational-state.json",
            "trajectory.jsonl",
            "research-plan.json",
        }
        else workspace
    )
    return root / name


@pytest.fixture
def published_run(tmp_path, monkeypatch, request):
    for key in list(os.environ):
        if key.startswith(
            ("A3_MODEL_", "A3_CONTEXT_", "A3_SUMMARY_", "A3_WORKER_", "A3_UPSTREAM_")
        ):
            monkeypatch.delenv(key)
    root = tmp_path / "project"
    root.mkdir()

    def git(*arguments):
        return subprocess.check_output(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *arguments,
            ],
            text=True,
        ).strip()

    git("init", "--quiet")
    source = root / "src/a3_research_agent"
    (source / "harness").mkdir(parents=True)
    (source / "__init__.py").write_text("")
    (source / "token_count.py").write_text("TOKENIZER = 'fixture'\n")
    (source / "harness/agent.py").write_text("VERSION = 'old'\n")
    git("add", "src")
    git("commit", "--quiet", "-m", "old published source")
    previous = git("rev-parse", "HEAD")
    (source / "harness/agent.py").write_text(
        "from pathlib import Path\nPath('native-started.txt').write_text('new published source')\n"
    )
    (source / "harness/paths.py").write_text("NATIVE_PATHS = True\n")
    (source / "harness/state_paths.py").write_text(
        'LAYOUT_VERSION = 1\nCHECKPOINT = "checkpoint.json"\n'
    )
    git("add", "src")
    git("commit", "--quiet", "-am", "new published source")
    commit = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", commit)
    shutil.copytree(ROOT / "prompt", root / "prompt")
    shutil.copytree(ROOT / "skills", root / "skills", ignore=shutil.ignore_patterns("__pycache__"))
    run = root / "trajectories/run-existing"
    run.mkdir(parents=True)
    actor = "candidate-main"
    workspace = research_workspace(run, actor)
    workspace.mkdir(parents=True)
    protected = {
        "checkpoint.json": '[{"role":"user","content":"existing task and observations"}]',
        "experiments/lesson.md": '{"id":"learned","text":"actual research lesson"}\n',
        "scripts/train.py": "print('preserve research')\n",
        "research-plan.json": '{"version":1,"revision":5,"steps":{}}',
        "operational-state.json": '{"version":1,"jobs":{"existing-job":{"status":"running"}}}',
        "trajectory.jsonl": '{"original":"evidence"}\n',
        "pending-calls.json": "null",
    }
    for name, text in protected.items():
        path = fixture_file(run, workspace, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    pin_run(run, previous)
    candidate = run / "sessions" / actor
    candidate.mkdir(parents=True)
    (candidate / "workspace").symlink_to(workspace, target_is_directory=True)
    record = {
        "owner": actor,
        "session_id": actor,
        "closed": False,
        "version": 1,
        "backend": "process",
        "exit_archived": True,
        "terminal_state": {"status": "exited", "exit_code": 0},
        "inputs": str(run / "inputs"),
    }
    (candidate / "session.json").write_text(json.dumps(record))
    state = {
        "version": 1,
        "backend": "process",
        "harness_commit": previous,
        "run_id": run.name,
        "status": "suspended",
        "model": "old/model",
        "endpoint": "http://127.0.0.1:34567",
        "gateway": {
            "sessions": {"root-token": run.name, "main-token": actor},
            "root_owners": [run.name],
            "scope_parents": {actor: run.name},
            "fixtures": {},
            "session_records": {
                actor: {"owner": actor, "closed": False, "parent": run.name, "token": "main-token"}
            },
            "main_owner": actor,
            "jobs": {actor: ["owned-job"]},
        },
    }
    RunStateStore(run).save(state)
    inputs = InputVersions(root, run)
    inputs.activate(inputs.prepare(), "start")
    (run / "startup.json").write_text(
        json.dumps(
            {
                "run_id": run.name,
                "session_id": actor,
                "model": "old/model",
                "harness_commit": previous,
            }
        )
    )
    handoff = tmp_path / "handoff.md"
    handoff.write_text("## 已核对结论\n\n原有结果保留。\n\n## 待办\n\n继续当前研究。\n")
    proposal = prepare_release(root, handoff, commit=commit, run=run)
    publish_release(run, proposal, proposal["digest"])
    release = night_state_directory(run) / "night-releases" / proposal["digest"]
    docker_calls = []
    environment = {"MODEL_NAME": "new/model"}
    return (
        root,
        run,
        workspace,
        previous,
        commit,
        proposal,
        release,
        protected,
        environment,
        docker_calls,
    )


def test_apply_release_keeps_run_owner_workspace_and_complete_research_state(published_run):
    from a3_research_agent.host.releases import apply_release

    _root, run, workspace, previous, commit, proposal, release, protected, env, calls = (
        published_run
    )
    original_startup = (run / "startup.json").read_bytes()
    outcome = apply_release(run, release, proposal["digest"], environment=env)
    assert outcome["status"] == "complete"
    assert run_commit(run) == commit
    assert (run / "startup.json").read_bytes() == original_startup
    assert all(
        fixture_file(run, workspace, name).read_text() == text for name, text in protected.items()
    )
    saved = RunStateStore(run).load()
    assert saved["status"] == "suspended" and saved["model"] == "new/model"
    assert saved["gateway"]["main_owner"] == "candidate-main"
    assert saved["gateway"]["jobs"] == {"candidate-main": ["owned-job"]}
    assert load_resume_state(run, RunStateStore(run), "new/model") == saved
    with pytest.raises(ValueError, match="model"):
        load_resume_state(run, RunStateStore(run), "old/model")
    archive = run / ".host/release-upgrades" / outcome["upgrade_id"]
    assert (archive / "checkpoint.json").read_text() == protected["checkpoint.json"]
    assert json.loads((archive / "state-before.json").read_text())["model"] == "old/model"
    assert json.loads((archive / "pin-before.json").read_text())["commit"] == previous
    assert not (run / ".host/pending-release.json").exists()
    assert [p.name for p in run.parent.iterdir() if p.is_dir()] == [run.name]
    assert not any(call[0] in {"unpause", "start", "create"} for call in calls)
    before = len(calls)
    assert apply_release(run, release, proposal["digest"], environment=env) == outcome
    assert len(calls) == before


def test_apply_release_preserves_an_empty_initial_checkpoint(published_run):
    from a3_research_agent.host.releases import apply_release

    _, run, workspace, _, commit, proposal, release, protected, env, _ = published_run
    checkpoint = b"[\n]\n"
    (run / "conversation/checkpoint.json").parent.mkdir(parents=True, exist_ok=True)
    (run / "conversation/checkpoint.json").write_bytes(checkpoint)
    (run / "conversation/pending-calls.json").parent.mkdir(parents=True, exist_ok=True)
    (run / "conversation/pending-calls.json").write_text("null")
    outcome = apply_release(run, release, proposal["digest"], environment=env)
    assert outcome["status"] == "complete"
    assert run_commit(run) == commit
    assert (run / "conversation/checkpoint.json").read_bytes() == checkpoint
    assert (run / "conversation/pending-calls.json").read_text() == "null"
    archive = run / ".host/release-upgrades" / outcome["upgrade_id"]
    assert (archive / "checkpoint.json").read_bytes() == checkpoint
    assert all(
        fixture_file(run, workspace, name).read_text() == content
        for name, content in protected.items()
        if name not in {"checkpoint.json", "pending-calls.json"}
    )
    assert RunStateStore(run).load()["status"] == "suspended"


def test_published_night_release_resumes_same_run_instead_of_making_a_fresh_task(
    published_run, monkeypatch
):
    _, run, _workspace, _, commit, proposal, _, protected, env, _ = published_run
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    command = ["python", "-m", "launch", "--resume-from", str(run)]
    schedule = NightSchedule(run, command)
    selected, target, extra = schedule.select()
    assert selected == command and target == run and extra == {}
    assert schedule.select() == (command, run, {})
    assert run_commit(run) == commit
    assert (run / "conversation/checkpoint.json").read_text() == protected["checkpoint.json"]
    assert json.loads(schedule.state.read_text())["release"] == proposal["digest"]


def test_release_refuses_a_live_run_or_unreconciled_checkpoint_before_container_changes(
    published_run,
):
    from a3_research_agent.host.releases import apply_release

    _, run, _workspace, _, _, proposal, release, _, env, calls = published_run
    with RunLease(run), pytest.raises(RuntimeError, match="Host"):
        apply_release(run, release, proposal["digest"], environment=env)
    assert not calls
    RunStateStore(run).update(status="running")
    with pytest.raises(ValueError, match="suspended"):
        apply_release(run, release, proposal["digest"], environment=env)
    RunStateStore(run).update(status="suspended")
    (run / "conversation/checkpoint.json").parent.mkdir(parents=True, exist_ok=True)
    (run / "conversation/checkpoint.json").write_text(
        json.dumps(
            [
                {"type": "function_call", "call_id": "uncertain", "name": "gateway"},
            ]
        )
    )
    with pytest.raises(ValueError, match="tool"):
        apply_release(run, release, proposal["digest"], environment=env)
    assert not calls


def test_release_keeps_an_unfinished_journal_without_replaying_its_tool(published_run):
    from a3_research_agent.host.releases import apply_release

    _, run, _workspace, _, _, proposal, release, _, env, calls = published_run
    journal = {
        "base_hash": hashlib.sha256(
            (run / "conversation/checkpoint.json").read_bytes()
        ).hexdigest(),
        "output": [
            {
                "type": "function_call",
                "call_id": "submitted",
                "name": "gateway",
                "arguments": '{"method":"POST","path":"/jobs"}',
            }
        ],
        "results": {},
    }
    (run / "conversation/pending-calls.json").parent.mkdir(parents=True, exist_ok=True)
    (run / "conversation/pending-calls.json").write_text(json.dumps(journal))
    apply_release(run, release, proposal["digest"], environment=env)
    assert json.loads((run / "conversation/pending-calls.json").read_text()) == journal
    assert not any(call[0] in {"start", "create", "unpause"} for call in calls)


def test_interrupted_apply_fences_resume_and_retries_without_a_second_input_revision(
    published_run, monkeypatch
):
    from argparse import Namespace

    from a3_research_agent.host import launch as bootstrap
    from a3_research_agent.host import releases

    root, run, _workspace, _, commit, proposal, release, protected, env, _ = published_run
    replace = releases.replace_run_pin
    monkeypatch.setattr(
        releases, "replace_run_pin", lambda *args: (_ for _ in ()).throw(OSError("interrupted pin"))
    )
    with pytest.raises(OSError, match="interrupted pin"):
        releases.apply_release(run, release, proposal["digest"], environment=env)
    assert (run / ".host/pending-release.json").exists()
    revision = json.loads((run / "inputs/input-manifest.json").read_text())["revision"]
    with pytest.raises(ValueError, match="release is pending"):
        bootstrap.launch_run(Namespace(resume_from=run), run, root, None)
    monkeypatch.setattr(releases, "replace_run_pin", replace)
    result = releases.apply_release(run, release, proposal["digest"], environment=env)
    assert result["commit"] == commit and result["input_revision"] == revision
    assert (run / "conversation/checkpoint.json").read_text() == protected["checkpoint.json"]
    feedback = json.loads((run / ".host/human-feedback/state.json").read_text())
    assert len(feedback["records"]) == 1


def test_explicit_apply_can_change_only_the_model_and_does_not_duplicate_feedback(published_run):
    from a3_research_agent.host.models.settings import require_release_model
    from a3_research_agent.host.releases import apply_release

    _, run, _, _, _, proposal, release, _, env, _ = published_run
    env = {
        **env,
        "A3_UPSTREAM_RESPONSES_BASE_URL": "https://provider.invalid/v1",
        "A3_UPSTREAM_RESPONSES_MODEL": "open-model-a",
        "A3_UPSTREAM_RESPONSES_API_KEY": "PRIVATE_SECRET",
    }
    first = apply_release(run, release, proposal["digest"], environment=env)
    changed = {**env, "A3_UPSTREAM_RESPONSES_MODEL": "open-model-b"}
    with pytest.raises(ValueError, match="released upstream model"):
        require_release_model(RunStateStore(run).load(), changed)
    second = apply_release(run, release, proposal["digest"], environment=changed)
    require_release_model(RunStateStore(run).load(), changed)
    assert second["upgrade_id"] != first["upgrade_id"]
    feedback = json.loads((run / ".host/human-feedback/state.json").read_text())
    assert len(feedback["records"]) == 1
    assert "PRIVATE_SECRET" not in json.dumps(second)


def test_real_native_host_resume_executes_new_source_in_the_same_workspace(
    published_run, monkeypatch
):
    import socket
    import time
    from argparse import Namespace

    from a3_research_agent.host import launch as bootstrap
    from a3_research_agent.host.releases import apply_release

    root, run, workspace, _, commit, proposal, release, protected, env, _ = published_run
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{probe.getsockname()[1]}"
    RunStateStore(run).update(endpoint=endpoint)
    (run / "inputs/deployment.json").write_text(json.dumps({"gateway": endpoint, "sessions": {}}))
    (run / "public/a3").mkdir(parents=True)
    apply_release(run, release, proposal["digest"], environment=env)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("A3_WEB_ENABLED", "0")
    monkeypatch.setenv("A3_REMOTE_RUN_STORAGE_BYTES", "0")
    monkeypatch.delenv("A3_UPSTREAM_RESPONSES_BASE_URL", raising=False)
    monkeypatch.setattr(
        bootstrap, "remote_call", lambda *args: pytest.fail("Unexpected remote call")
    )
    original_startup = (run / "startup.json").read_bytes()
    original_server = bootstrap.uvicorn.Server
    servers = []

    class ObservedServer(original_server):
        def __init__(self, *arguments, **options):
            super().__init__(*arguments, **options)
            servers.append(self)

    monkeypatch.setattr(bootstrap.uvicorn, "Server", ObservedServer)

    class Supervisor:
        def __init__(self, **options):
            pass

        def run(self):
            assert {
                sock.getsockname()[0]
                for listener in servers[0].servers
                for sock in listener.sockets
            } == {"127.0.0.1"}
            deadline = time.monotonic() + 3
            while not (workspace / "native-started.txt").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (workspace / "native-started.txt").read_text() == "new published source"
            return {"status": "incomplete"}

    monkeypatch.setattr(bootstrap, "RunSupervisor", Supervisor)
    bootstrap.launch_run(Namespace(resume_from=run, verify_deployment=False), run, root, None)
    record = json.loads((run / "sessions/candidate-main/session.json").read_text())
    assert record["backend"] == "process" and record["version"] == 1
    assert record["owner"] == "candidate-main"
    assert (run / "startup.json").read_bytes() == original_startup
    assert all(
        fixture_file(run, workspace, name).read_text() == text for name, text in protected.items()
    )
    assert RunStateStore(run).load()["status"] == "suspended"
    deployment = json.loads((run / "inputs/deployment.json").read_text())
    assert deployment["harness_commit"] == commit and deployment["local_execution"] == "process"


def test_native_resume_fences_unknown_process_before_inputs_gateway_or_model_setup(
    published_run, monkeypatch
):
    from argparse import Namespace

    from a3_research_agent.host import launch as bootstrap
    from a3_research_agent.host.releases import apply_release
    from a3_research_agent.host.sessions import processes

    root, run, _, _, _, proposal, release, _, env, _ = published_run
    apply_release(run, release, proposal["digest"], environment=env)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("A3_UPSTREAM_RESPONSES_BASE_URL", raising=False)
    before_state = RunStateStore(run).path.read_bytes()
    before_inputs = (run / "inputs/input-manifest.json").read_bytes()
    observed = []

    class FencedRuntime(processes.ProcessRuntime):
        def preflight_resume(self, owner):
            observed.append(owner)
            raise RuntimeError("Prior native process is still live")

    monkeypatch.setattr(bootstrap, "ProcessRuntime", FencedRuntime)
    monkeypatch.setattr(
        bootstrap,
        "configured_upstream",
        lambda *args: pytest.fail("Model setup preceded preflight"),
    )
    monkeypatch.setattr(
        bootstrap, "create_app", lambda *args, **kwargs: pytest.fail("Gateway preceded preflight")
    )
    with pytest.raises(RuntimeError, match="still live"):
        bootstrap.launch_run(Namespace(resume_from=run, verify_deployment=False), run, root, None)
    assert observed == ["candidate-main"]
    assert RunStateStore(run).path.read_bytes() == before_state
    assert (run / "inputs/input-manifest.json").read_bytes() == before_inputs


@pytest.mark.parametrize("fault", [None, "startup.json", "startup-receipt.json"])
def test_real_fresh_published_host_saves_provenance_before_native_start(
    published_run, tmp_path, monkeypatch, fault
):
    import time
    from argparse import Namespace

    from a3_research_agent.host import launch as bootstrap
    from a3_research_agent.host.published_harness import COMMAND
    from a3_research_agent.host.runs.continuation import HarnessStart

    root, _, _, _, commit, _, _, _, env, _ = published_run
    shutil.rmtree(root / "workspace")
    a3 = tmp_path / "a3-reference"
    a3.mkdir()
    (a3 / "README.md").write_text("Read-only simulation reference fixture.\n")
    subprocess.run(["git", "init", "--quiet", str(a3)], check=True)
    subprocess.run(["git", "-C", str(a3), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(a3),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "reference",
        ],
        check=True,
    )
    run = root / "trajectories/run-fresh"
    run.mkdir()
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("A3_WEB_ENABLED", "0")
    monkeypatch.setenv("A3_REMOTE_RUN_STORAGE_BYTES", "0")
    monkeypatch.delenv("A3_UPSTREAM_RESPONSES_BASE_URL", raising=False)
    monkeypatch.setattr(
        bootstrap, "remote_call", lambda *args: pytest.fail("Unexpected remote job call")
    )

    class Supervisor:
        def __init__(self, **options):
            self.workspace = options["result_workspace"]

        def run(self):
            workspace = self.workspace()
            deadline = time.monotonic() + 3
            while not (workspace / "native-started.txt").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (workspace / "native-started.txt").read_text() == "new published source"
            return {"status": "incomplete"}

    monkeypatch.setattr(bootstrap, "RunSupervisor", Supervisor)
    start = HarnessStart(COMMAND.copy(), {"checkpoint.json": "[]"}, harness_commit=commit)
    original_write = bootstrap.write_durable
    creates = []
    original_create = bootstrap.ProcessRuntime.create

    def observed_create(self, *args, **kwargs):
        creates.append(True)
        return original_create(self, *args, **kwargs)

    monkeypatch.setattr(bootstrap.ProcessRuntime, "create", observed_create)

    def interrupted_write(path, content):
        if path.name == fault:
            raise OSError("controlled startup storage failure")
        return original_write(path, content)

    monkeypatch.setattr(bootstrap, "write_durable", interrupted_write)

    def launch():
        bootstrap.launch_run(
            Namespace(
                resume_from=None,
                verify_deployment=False,
                run_harness=None,
                harness_commit=commit,
                input_proposal=InputVersions(root, None).prepare(),
            ),
            run,
            a3,
            start,
        )

    if fault:
        with pytest.raises(OSError, match="controlled startup"):
            launch()
        if fault == "startup.json":
            assert not creates
            assert not (run / "startup.json").exists()
            return
        assert creates
    else:
        launch()
    saved = RunStateStore(run).load()
    actor = saved["gateway"]["main_owner"]
    record = json.loads((run / "sessions" / actor / "session.json").read_text())
    assert record["backend"] == "process" and record["version"] == 1
    assert saved["status"] == "suspended"
    assert json.loads((run / "startup.json").read_text())["harness_commit"] == commit
    assert load_resume_state(run, RunStateStore(run), env["MODEL_NAME"])["status"] == "suspended"


@pytest.mark.parametrize(
    "changed",
    [
        {"A3_MODEL_REASONING_EFFORT": "high"},
        {"A3_SUMMARY_MODEL_NAME": "provider/summary", "A3_SUMMARY_CONTEXT_TOKENS": "500000"},
        {"A3_MODEL_CONTEXT_TOKENS": "600000"},
    ],
)
def test_plain_resume_rejects_changed_released_inference_settings(published_run, changed):
    from a3_research_agent.host.models.settings import require_release_model
    from a3_research_agent.host.releases import apply_release

    _, run, _, _, _, proposal, release, _, env, _ = published_run
    apply_release(run, release, proposal["digest"], environment=env)
    state = RunStateStore(run).load()
    with pytest.raises(ValueError, match="released model settings"):
        require_release_model(state, {**env, **changed})
    result = apply_release(run, release, proposal["digest"], environment={**env, **changed})
    require_release_model(RunStateStore(run).load(), {**env, **changed})
    assert result["model_settings"] != {key: state[key] for key in result["model_settings"]}


def test_night_release_cli_derives_project_from_anchor_when_installed(
    published_run, tmp_path, monkeypatch, capsys
):
    import sys

    from a3_research_agent.cli import night_release

    root, run, _, _, commit, _, _, _, _, _ = published_run
    handoff = tmp_path / "release-note.md"
    handoff.write_text("## 已核对结论\n\n维护内容已验证。\n\n## 待办\n\n接续原研究状态。\n")
    monkeypatch.setattr(
        night_release, "__file__", "/installed/site-packages/a3_research_agent/cli/night_release.py"
    )
    monkeypatch.setattr(
        sys, "argv", ["night-release", str(run), "--commit", commit, "--handoff", str(handoff)]
    )
    night_release.main()
    assert "Release digest:" in capsys.readouterr().out
    assert (root / ".runtime/published-harness").is_dir()


def test_release_after_trajectory_rename_keeps_workspace_mapping(published_run):
    from a3_research_agent.host.published_harness import MARKER, run_identity

    root, run, workspace, _, commit, *_ = published_run
    protected = {
        name: fixture_file(run, workspace, name).read_bytes()
        for name in (
            "checkpoint.json",
            "experiments/lesson.md",
            "scripts/train.py",
            "trajectory.jsonl",
        )
    }
    original_identity = run_identity(run)
    relocated = run.with_name("2026-11-10_21-27-31-654793")
    run.rename(relocated)
    handoff = root / "relocation-handoff.md"
    handoff.write_text("## 已核对结论\n\n保留既有研究状态。\n\n## 待办\n\n继续研究。\n")
    proposal = prepare_release(root, handoff, commit=commit, run=relocated)
    publish_release(relocated, proposal, proposal["digest"])
    directory = night_state_directory(relocated) / "night-releases" / proposal["digest"]
    from a3_research_agent.host.releases import apply_release

    apply_release(relocated, directory, proposal["digest"], environment={"MODEL_NAME": "new/model"})
    marker = json.loads((relocated / MARKER).read_text())
    assert marker["run_id"] == original_identity
    assert "workspace_root" not in marker
    assert research_workspace(relocated, "candidate-main") == workspace
    for name, content in protected.items():
        assert fixture_file(relocated, workspace, name).read_bytes() == content
