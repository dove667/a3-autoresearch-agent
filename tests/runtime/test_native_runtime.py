"""Published local Harness execution needs neither Docker nor workspace imports."""

import json
import time
from pathlib import Path

import pytest

from a3_research_agent.host.published_harness import COMMAND


def runtime(tmp_path, monkeypatch):
    from a3_research_agent.host.sessions.processes import ProcessRuntime

    source = tmp_path / "published"
    package = source / "a3_research_agent/harness"
    package.mkdir(parents=True)
    (source / "a3_research_agent/__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    from a3_research_agent.harness import paths, state_paths

    (package / "paths.py").write_text(Path(paths.__file__).read_text())
    (package / "state_paths.py").write_text(Path(state_paths.__file__).read_text())
    (package / "agent.py").write_text("""import json, os, pathlib, signal, sys, time
root=pathlib.Path.cwd()
def stop(*args):
 (root/'interrupted.json').write_text('saved')
 sys.exit(0)
signal.signal(signal.SIGINT, stop)
with (root/'starts.jsonl').open('a') as stream:
 stream.write(json.dumps({'file':__file__, 'environment':dict(os.environ), 'path':sys.path})+'\\n')
print('native child ready', flush=True)
while True: time.sleep(.05)
""")
    monkeypatch.setenv("OPENAI_API_KEY", "host-secret")
    monkeypatch.setenv("TAVILY_API_KEY", "host-secret")
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    public = tmp_path / "reference"
    public.mkdir()
    return ProcessRuntime(
        root=tmp_path / "trajectories/run-unit/sessions",
        inputs=inputs,
        public_a3=public,
        endpoint="http://127.0.0.1:49123",
        published_source=source,
    )


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Native child did not reach the expected state")


def launch(local, extra_files=None):
    local.create(
        "main",
        COMMAND,
        {".agent/checkpoint.json": '[{"role":"user","content":"task"}]', **(extra_files or {})},
        "scoped-token",
    )
    workspace = (local.root / "main/workspace").resolve()
    wait_for(lambda: (workspace / "starts.jsonl").is_file())
    return workspace


def test_native_runtime_rejects_arbitrary_commands_and_nonlocal_gateway(tmp_path, monkeypatch):
    local = runtime(tmp_path, monkeypatch)
    with pytest.raises(PermissionError):
        local.create("main", ["python", "-c", "raise RuntimeError('untrusted')"], {}, "token")
    assert not local.records
    from a3_research_agent.host.sessions.processes import ProcessRuntime

    with pytest.raises(ValueError, match="loopback"):
        ProcessRuntime(
            root=tmp_path / "other",
            inputs=local.inputs,
            public_a3=local.public_a3,
            endpoint="http://host.docker.internal:9000",
            published_source=local.published_source,
        )
    (local.published_source / "a3_research_agent/harness/paths.py").unlink()
    with pytest.raises(ValueError, match="source is incomplete"):
        ProcessRuntime.require_native_source(local.published_source)


def test_native_run_has_one_process_and_owner_scoped_access(tmp_path, monkeypatch):
    from a3_research_agent.host.sessions.workspace import SessionCapacityError

    local = runtime(tmp_path, monkeypatch)
    try:
        launch(local)
        with pytest.raises(SessionCapacityError):
            local.create("other", COMMAND, {}, "other-token")
        assert not (local.root / "other").exists()
        with pytest.raises(PermissionError):
            local.artifact("other", "main", ".agent/checkpoint.json")
        with pytest.raises(PermissionError):
            local.read_workspace_files("other", [])
    finally:
        local.close()


def test_native_natural_exit_is_archived_without_deleting_research(tmp_path, monkeypatch):
    from a3_research_agent.host.sessions.workspace import exit_archived

    local = runtime(tmp_path, monkeypatch)
    (local.published_source / "a3_research_agent/harness/agent.py").write_text(
        "print('finished', flush=True)\n"
    )
    local.create(
        "main", COMMAND, {".agent/checkpoint.json": '[{"role":"user","content":"task"}]'}, "token"
    )
    wait_for(lambda: local.status("main", "main")["status"] == "exited")
    local.reap_exited()
    record = local.records["main"]
    assert exit_archived(record) and record["terminal_state"]["exit_code"] == 0
    assert "finished" in local.status("main", "main")["output"]
    assert (local.root / "main/workspace/.agent/checkpoint.json").is_file()
    local.close()


def test_native_child_uses_fixed_source_scoped_env_and_keeps_logs(tmp_path, monkeypatch):
    local = runtime(tmp_path, monkeypatch)
    try:
        workspace = launch(
            local,
            {
                "json.py": "raise RuntimeError('workspace import')",
                "a3_research_agent/__init__.py": "raise RuntimeError('workspace import')",
            },
        )
        record = json.loads((workspace / "starts.jsonl").read_text())
        assert Path(record["file"]).is_relative_to(local.published_source)
        assert "host-secret" not in str(record)
        assert record["environment"]["A3_GATEWAY_TOKEN"] == "scoped-token"
        assert record["environment"]["A3_WORKSPACE"] == str(workspace)
        assert workspace == tmp_path / "workspace"
        assert record["environment"]["A3_MEMORY_ROOT"] == str(tmp_path / "memory")
        assert (tmp_path / "memory").is_dir()
        assert record["environment"]["A3_INPUT_ROOT"] == str(local.inputs)
        assert "A3_GOAL_FILE" not in record["environment"]
        assert "A3_ACCEPTANCE_FILE" not in record["environment"]
        assert record["environment"]["A3_RUN_DIR"] == str(workspace / "results")
        assert local.console_path("main").is_relative_to(tmp_path / "runs/run-unit")
        assert not (local.root / "main/console.log").exists()
        assert str(workspace) not in record["path"]
        assert local.status("main", "main")["status"] == "running"
        assert local.stop("main", "main")["status"] == "exited"
        assert (workspace / "interrupted.json").read_text() == "saved"
        assert "native child ready" in local.status("main", "main")["output"]
        assert local.records["main"]["self_stopped"] is True
    finally:
        local.close()


def test_new_process_cannot_overwrite_the_shared_research_workspace(tmp_path, monkeypatch):
    local = runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    checkpoint = workspace / ".agent/checkpoint.json"
    checkpoint.parent.mkdir()
    checkpoint.write_text('[{"role":"user","content":"existing work"}]')
    before = checkpoint.read_bytes()
    with pytest.raises(ValueError, match="must be resumed"):
        local.create("main", COMMAND, {".agent/checkpoint.json": "[]"}, "token")
    assert checkpoint.read_bytes() == before
    assert not local.records
    assert not (local.root / "main").exists()


def test_native_artifact_and_schema_use_only_current_session_contract(tmp_path, monkeypatch):
    import base64

    from a3_research_agent.host.sessions.workspace import exit_archived

    local = runtime(tmp_path, monkeypatch)
    try:
        workspace = launch(local)
        (workspace / "note.txt").write_text("native evidence")
        result = local.artifact("main", "main", "note.txt")
        assert base64.b64decode(result["data"]) == b"native evidence"
        assert local.records["main"]["version"] == 1
        assert local.records["main"]["backend"] == "process"
        assert "container_removed" not in local.records["main"]
        assert "container_id" not in local.records["main"]
        local.stop("main", "main")
        assert exit_archived(local.records["main"]) is True
    finally:
        local.close()


def test_unsupported_session_schema_is_rejected_without_rewriting_it(tmp_path, monkeypatch):
    local = runtime(tmp_path, monkeypatch)
    directory = local.root / "unknown"
    directory.mkdir()
    path = directory / "session.json"
    path.write_text('{"owner":"unknown", "session_id":"unknown", "backend":"unsupported"}')
    original = path.read_bytes()
    with pytest.raises(ValueError, match="Unsupported local session record schema"):
        local.load_records()
    assert path.read_bytes() == original


def test_native_suspend_resume_retains_workspace_and_unknown_tool_journal(tmp_path, monkeypatch):
    local = runtime(tmp_path, monkeypatch)
    try:
        workspace = launch(local)
        unknown = '{"base_hash":"old","output":[{"type":"function_call","call_id":"pending"}],"results":{}}'
        (workspace / ".agent/pending-calls.json").parent.mkdir(parents=True, exist_ok=True)
        (workspace / ".agent/pending-calls.json").write_text(unknown)
        local.suspend()
        assert (workspace / "interrupted.json").is_file()
        assert local.state(local.records["main"])["status"] == "exited"
        local.resume_sessions({"new-token": "main"}, "main")
        wait_for(lambda: len((workspace / "starts.jsonl").read_text().splitlines()) == 2)
        assert (workspace / ".agent/pending-calls.json").read_text() == unknown
        assert (
            workspace / ".agent/checkpoint.json"
        ).read_text() == '[{"role":"user","content":"task"}]'
        assert local.status("main", "main")["status"] == "running"
        assert (
            json.loads((workspace / "starts.jsonl").read_text().splitlines()[-1])["environment"][
                "A3_GATEWAY_TOKEN"
            ]
            == "new-token"
        )
    finally:
        local.close()


def test_native_suspend_resume_keeps_an_empty_initial_checkpoint(tmp_path, monkeypatch):
    local = runtime(tmp_path, monkeypatch)
    try:
        workspace = launch(
            local, {".agent/checkpoint.json": "[\n]\n", ".agent/pending-calls.json": "null"}
        )
        checkpoint = (workspace / ".agent/checkpoint.json").read_bytes()
        local.suspend()
        assert local.state(local.records["main"])["status"] == "exited"
        assert (workspace / ".agent/checkpoint.json").read_bytes() == checkpoint
        local.resume_sessions({"new-token": "main"}, "main")
        wait_for(lambda: len((workspace / "starts.jsonl").read_text().splitlines()) == 2)
        assert local.status("main", "main")["status"] == "running"
        assert (workspace / ".agent/checkpoint.json").read_bytes() == checkpoint
        assert (workspace / ".agent/pending-calls.json").read_text() == "null"
        assert not (workspace / "task.txt").exists()
    finally:
        local.close()


def test_native_runtime_never_signals_unowned_live_process(tmp_path, monkeypatch):
    local = runtime(tmp_path, monkeypatch)
    try:
        launch(local)
        restored = type(local)(
            root=local.root,
            inputs=local.inputs,
            public_a3=local.public_a3,
            endpoint=local.endpoint,
            published_source=local.published_source,
        )
        restored.load_records()
        with pytest.raises(RuntimeError, match="previous Host"):
            restored.cancel("main", "main")
        assert local.status("main", "main")["status"] == "running"
    finally:
        local.close()


def test_native_preflight_rejects_live_orphan_without_writing_or_signaling(tmp_path, monkeypatch):
    local = runtime(tmp_path, monkeypatch)
    try:
        launch(local)
        restored = type(local)(
            root=local.root,
            inputs=local.inputs,
            public_a3=local.public_a3,
            endpoint=local.endpoint,
            published_source=local.published_source,
        )
        restored.load_records()
        record_path = local.root / "main/session.json"
        original = record_path.read_bytes()
        monkeypatch.setattr(
            restored, "save_record", lambda *args: pytest.fail("preflight wrote state")
        )
        monkeypatch.setattr(
            restored, "create", lambda *args, **kwargs: pytest.fail("preflight started a process")
        )
        monkeypatch.setattr(
            restored, "_stop_process", lambda *args: pytest.fail("preflight signaled a process")
        )
        with pytest.raises(RuntimeError, match="previous Host"):
            restored.preflight_resume("main")
        assert record_path.read_bytes() == original
        assert local.status("main", "main")["status"] == "running"
    finally:
        local.close()


def test_native_preflight_accepts_exited_main_and_checks_nonmain_sessions(tmp_path, monkeypatch):
    local = runtime(tmp_path, monkeypatch)
    launch(local)
    local.suspend()
    original = (local.root / "main/session.json").read_bytes()
    local.preflight_resume("main")
    assert (local.root / "main/session.json").read_bytes() == original
    local.records["old"] = {
        "version": 1,
        "owner": "old",
        "session_id": "old",
        "backend": "process",
        "launch_pending": True,
        "closed": True,
    }
    with pytest.raises(RuntimeError, match="uncertain"):
        local.preflight_resume("main")
    local.records.pop("old")
    local.close()


def test_native_preflight_refuses_uncertain_launch_before_gateway_recovery(tmp_path, monkeypatch):
    local = runtime(tmp_path, monkeypatch)
    local.records["main"] = {
        "version": 1,
        "owner": "main",
        "session_id": "main",
        "backend": "process",
        "launch_pending": True,
        "closed": False,
    }
    with pytest.raises(RuntimeError, match="uncertain"):
        local.preflight_resume("main")


def test_saved_native_process_identity_does_not_depend_on_host_timezone(monkeypatch):
    import os

    from a3_research_agent.host.sessions.processes import process_identity

    monkeypatch.setenv("TZ", "UTC")
    original = process_identity(os.getpid())
    monkeypatch.setenv("TZ", "Etc/GMT+12")
    assert process_identity(os.getpid()) == original


def test_native_restart_after_host_exit_reuses_durable_records(tmp_path, monkeypatch):
    local = runtime(tmp_path, monkeypatch)
    workspace = launch(local)
    local.suspend()
    restored = type(local)(
        root=local.root,
        inputs=local.inputs,
        public_a3=local.public_a3,
        endpoint=local.endpoint,
        published_source=local.published_source,
    )
    try:
        restored.load_records()
        restored.resume_sessions({"second-token": "main"}, "main")
        wait_for(lambda: len((workspace / "starts.jsonl").read_text().splitlines()) == 2)
        assert restored.status("main", "main")["status"] == "running"
        (workspace / "research").mkdir()
        source = workspace / "research/example.py"
        source.write_text("print('remote only')")
        import hashlib

        files, manifest = restored.read_workspace_files(
            "main",
            [
                {
                    "path": "research/example.py",
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                }
            ],
        )
        assert files == {"research/example.py": "print('remote only')"}
        assert manifest[0]["bytes"] == 20
    finally:
        restored.close()


def test_interrupted_tool_retains_journal_and_recovery_never_replays(tmp_path, monkeypatch):
    from a3_research_agent.harness import agent

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("A3_GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("A3_GATEWAY_TOKEN", "scoped-token")
    monkeypatch.setenv("A3_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("A3_INPUT_ROOT", str(tmp_path / "inputs"))
    monkeypatch.setenv("A3_REFERENCE_ROOT", str(tmp_path / "reference"))
    monkeypatch.setattr(agent.sys, "argv", ["agent", "--resume"])
    (tmp_path / ".agent/checkpoint.json").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".agent/checkpoint.json").write_text("[]")

    class Compactor:
        def __init__(self, *args, **kwargs):
            pass

        def before_request(self, messages, **kwargs):
            return messages, None

        def wait(self, **kwargs):
            return None

        def apply_ready(self, messages):
            return messages, None

    monkeypatch.setattr(agent.compaction, "ForkCompactor", Compactor)
    monkeypatch.setattr(agent.context_manager, "configure", lambda req: None)
    monkeypatch.setattr(agent, "req", lambda *args, **kwargs: {"is_main": True})
    monkeypatch.setattr(
        agent,
        "request_model",
        lambda request: {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "job_submit",
                    "call_id": "one",
                    "arguments": "{}",
                }
            ],
        },
    )
    executed = []

    def interrupted(*args):
        executed.append("submitted")
        raise KeyboardInterrupt

    monkeypatch.setattr(agent, "tool", interrupted)
    agent.main()
    before = json.loads((tmp_path / ".agent/checkpoint.json").read_text())
    journal = json.loads((tmp_path / ".agent/pending-calls.json").read_text())
    assert journal["results"] == {} and journal["output"][0]["call_id"] == "one"
    restored = agent.recover_journal(before)
    assert executed == ["submitted"]
    assert json.loads(restored[-1]["output"])["error"] == "outcome_unknown_after_interruption"
