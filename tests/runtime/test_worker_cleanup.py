"""Host cleanup must preserve evidence and recovery independently of Docker."""

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.sessions.containers import CandidateRuntime


class DockerFixture:
    """Replace only the external Docker CLI; use real files and runtime records."""

    def __init__(self):
        self.states = {}
        self.fail_logs = False
        self.fail_remove = False
        self.next_id = 0
        self.tokens = {}

    def __call__(self, *args, check=True):
        operation, container = args[0], args[-1]
        if operation == "create":
            owner = args[args.index("--name") + 1].removeprefix("a3-candidate-")
            self.tokens[owner] = next(
                value.split("=", 1)[1] for value in args if value.startswith("A3_GATEWAY_TOKEN=")
            )
            self.next_id += 1
            container = f"new-{self.next_id}"
            self.states[container] = {"Status": "created", "ExitCode": 0}
            return container
        if operation == "inspect":
            if container not in self.states:
                from a3_research_agent.host.sessions.containers import (
                    CandidateContainerMissingError,
                )

                raise CandidateContainerMissingError("Container no longer exists")
            return json.dumps(self.states[container])
        if operation == "logs":
            if self.fail_logs:
                raise RuntimeError("Log export failed")
            return "worker output\n"
        if operation == "rm":
            if self.fail_remove:
                raise RuntimeError("Removal failed")
            # Deletion may only happen after the durable exit record and console exist.
            del self.states[container]
        elif operation == "stop":
            self.states[container] = {"Status": "exited", "ExitCode": 137}
        elif operation in {"start", "unpause"}:
            self.states[container]["Status"] = "running"
        elif operation == "pause":
            self.states[container]["Status"] = "paused"
        return ""


def runtime_fixture(tmp_path, monkeypatch):
    docker = DockerFixture()
    runtime = CandidateRuntime(
        root=tmp_path / "candidates",
        image="fixture",
        inputs=tmp_path,
        public_a3=tmp_path,
        endpoint="http://gateway",
    )
    monkeypatch.setattr(runtime, "docker", docker)
    return runtime, docker


def add_worker(runtime, docker, owner, status="exited"):
    directory = runtime.root / owner
    workspace = directory / "workspace"
    workspace.mkdir(parents=True)
    for name, content in {
        "agent.py": "pass\n",
        "checkpoint.json": '[{"role":"user","content":"original context"}]',
        "memory.jsonl": '{"lesson":"keep"}\n',
        "result.txt": "experiment evidence",
        "resume.json": json.dumps(
            {
                "command": ["python", "agent.py", "--resume"],
                "files": ["agent.py", "checkpoint.json"],
            }
        ),
    }.items():
        (workspace / name).write_text(content)
    record = {"owner": owner, "session_id": owner, "container_id": owner, "closed": False}
    runtime.records[owner] = record
    (directory / "session.json").write_text(json.dumps(record))
    docker.states[owner] = {"Status": status, "ExitCode": 7, "FinishedAt": "2026-10-31T00:00:00Z"}
    return workspace


def test_exited_worker_is_removed_but_evidence_status_and_peers_survive(tmp_path, monkeypatch):
    runtime, docker = runtime_fixture(tmp_path, monkeypatch)
    workspace = add_worker(runtime, docker, "finished")
    add_worker(runtime, docker, "running", "running")
    add_worker(runtime, docker, "paused", "paused")
    before = {path.name: path.read_bytes() for path in workspace.iterdir()}
    runtime.reap_exited()
    assert set(docker.states) == {"running", "paused"}
    assert before == {path.name: path.read_bytes() for path in workspace.iterdir()}
    saved = json.loads((runtime.root / "finished/session.json").read_text())
    assert saved["container_removed"] is True
    assert saved["closed"] is False
    assert saved["terminal_state"]["ExitCode"] == 7
    assert (runtime.root / "finished/console.log").read_text() == "worker output\n"
    runtime.records.clear()
    runtime.load_records()
    status = runtime.status("finished", "finished")
    assert status["status"] == "exited" and status["exit_code"] == 7
    assert status["output"] == "worker output\n"
    artifact = runtime.artifact("finished", "finished", "result.txt")
    assert base64.b64decode(artifact["data"]) == b"experiment evidence"
    runtime.check_capacity()
    runtime.suspend()
    assert docker.states["running"]["Status"] == "paused"
    # Explicit later session closure must not try to inspect/delete the removed container.
    runtime.cancel("finished", "finished")
    assert runtime.status("finished", "finished")["status"] == "closed"


@pytest.mark.parametrize("failure", ["fail_logs", "fail_remove"])
def test_cleanup_failure_keeps_container_and_retries_after_host_restart(
    tmp_path, monkeypatch, failure
):
    runtime, docker = runtime_fixture(tmp_path, monkeypatch)
    add_worker(runtime, docker, "finished")
    setattr(docker, failure, True)
    runtime.reap_exited()
    assert "finished" in docker.states
    saved = json.loads((runtime.root / "finished/session.json").read_text())
    assert saved["cleanup_error"]
    assert not saved.get("container_removed")
    assert runtime.status("finished", "finished")["status"] == "exited"
    runtime.records.clear()
    runtime.load_records()
    setattr(docker, failure, False)
    runtime.reap_exited()
    assert "finished" not in docker.states
    assert not runtime.records["finished"].get("cleanup_error")


def test_remove_success_before_record_update_is_reconciled_without_losing_logs(
    tmp_path, monkeypatch
):
    runtime, docker = runtime_fixture(tmp_path, monkeypatch)
    add_worker(runtime, docker, "finished")
    docker.fail_remove = True
    runtime.reap_exited()
    assert runtime.records["finished"]["terminal_state"]["ExitCode"] == 7
    del docker.states["finished"]
    docker.fail_remove = False
    runtime.records.clear()
    runtime.load_records()
    runtime.reap_exited()
    assert runtime.records["finished"]["container_removed"] is True
    assert runtime.status("finished", "finished")["output"] == "worker output\n"


def test_removed_main_resumes_with_same_identity_workspace_context_and_memory(
    tmp_path, monkeypatch
):
    runtime, docker = runtime_fixture(tmp_path, monkeypatch)
    workspace = add_worker(runtime, docker, "main")
    add_worker(runtime, docker, "helper")
    checkpoint = (workspace / "checkpoint.json").read_bytes()
    runtime.reap_exited()
    runtime.records.clear()
    runtime.load_records()
    runtime.resume_sessions({"original-token": "main", "helper-token": "helper"}, "main")
    assert runtime.records["main"]["owner"] == "main"
    assert runtime.records["main"]["command"] == ["python", "agent.py", "--resume"]
    assert runtime.status("main", "main")["status"] == "running"
    assert (workspace / "checkpoint.json").read_bytes() == checkpoint
    assert (workspace / "memory.jsonl").read_text() == '{"lesson":"keep"}\n'
    assert runtime.status("helper", "helper")["status"] == "exited"
    assert len(docker.states) == 1
    assert list((runtime.root / "main").glob("console-resume-*.log"))


def test_gateway_reaps_without_agent_polling_and_preserves_other_worker_and_jobs(
    tmp_path, monkeypatch
):
    runtime, docker = runtime_fixture(tmp_path, monkeypatch)
    remote_calls = []
    app = create_app(
        model_name="fixture/model",
        sessions={"root-token": "root"},
        sandbox=runtime,
        usage_log=tmp_path / "usage.jsonl",
        forward_model=lambda **kw: {},
        remote_call=lambda op, payload: remote_calls.append(op) or {"job_id": "owned-job"},
    )
    root = {"Authorization": "Bearer root-token"}
    with TestClient(app) as client:
        parent = client.post("/sessions", headers=root, json={"command": ["true"]}).json()[
            "session_id"
        ]
        parent_auth = {"Authorization": "Bearer " + docker.tokens[parent]}
        child = client.post("/sessions", headers=parent_auth, json={"command": ["true"]}).json()[
            "session_id"
        ]
        child_auth = {"Authorization": "Bearer " + docker.tokens[child]}
        client.post("/jobs", headers=parent_auth, json={"command": ["true"]})
        docker.states[runtime.records[parent]["container_id"]]["Status"] = "exited"
        deadline = time.monotonic() + 5
        while not runtime.records[parent].get("container_removed") and time.monotonic() < deadline:
            time.sleep(0.02)
        assert runtime.records[parent].get("container_removed") is True
        assert client.get(f"/sessions/{parent}", headers=root).json()["status"] == "exited"
        assert client.get(f"/sessions/{child}", headers=root).json()["status"] == "running"
        assert client.get("/sessions/self", headers=child_auth).status_code == 200
        assert remote_calls == ["submit"]


def test_exit_record_must_be_saved_before_container_can_be_removed(tmp_path, monkeypatch):
    runtime, docker = runtime_fixture(tmp_path, monkeypatch)
    add_worker(runtime, docker, "finished")
    write = runtime.write_durable
    failed = False

    def fail_first_exit_record(path, content):
        nonlocal failed
        if path.name == "session.json" and "terminal_state" in content and not failed:
            failed = True
            raise OSError("Disk temporarily unavailable")
        write(path, content)

    monkeypatch.setattr(runtime, "write_durable", fail_first_exit_record)
    runtime.reap_exited()
    assert "finished" in docker.states
    assert runtime.records["finished"]["cleanup_error"] == "OSError"
    runtime.reap_exited()
    assert "finished" not in docker.states
    assert runtime.status("finished", "finished")["exit_code"] == 7


def test_self_stop_removes_only_caller_and_is_never_automatically_resumed(tmp_path, monkeypatch):
    runtime, docker = runtime_fixture(tmp_path, monkeypatch)
    add_worker(runtime, docker, "main", "running")
    add_worker(runtime, docker, "successor", "running")
    runtime.stop("main", "main")
    assert "main" not in docker.states
    assert docker.states["successor"]["Status"] == "running"
    runtime.records.clear()
    runtime.load_records()
    runtime.resume_sessions({"successor-token": "successor"}, "successor")
    assert set(docker.states) == {"successor"}
    assert runtime.records["main"]["self_stopped"] is True


def test_self_stop_requires_actual_docker_exit_before_removal(tmp_path, monkeypatch):
    runtime, docker = runtime_fixture(tmp_path, monkeypatch)
    add_worker(runtime, docker, "main", "running")

    def ineffective_stop(*args, **kwargs):
        if args[0] == "stop":
            return ""
        return docker(*args, **kwargs)

    monkeypatch.setattr(runtime, "docker", ineffective_stop)
    with pytest.raises(RuntimeError, match="not been confirmed"):
        runtime.stop("main", "main")
    assert docker.states["main"]["Status"] == "running"
    assert not runtime.records["main"].get("self_stopped")
    assert not (runtime.root / "main/console.log").exists()


def test_resume_reconciles_removal_crash_window_before_recreating_main(tmp_path, monkeypatch):
    runtime, docker = runtime_fixture(tmp_path, monkeypatch)
    add_worker(runtime, docker, "main")
    docker.fail_remove = True
    runtime.reap_exited()
    del docker.states["main"]
    docker.fail_remove = False
    runtime.records.clear()
    runtime.load_records()
    runtime.resume_sessions({"original-token": "main"}, "main")
    assert runtime.status("main", "main")["status"] == "running"
    assert list((runtime.root / "main").glob("console-resume-*.log"))


def test_resume_never_discards_unarchived_logs_or_mistakes_daemon_failure_for_loss(
    tmp_path, monkeypatch
):
    runtime, docker = runtime_fixture(tmp_path, monkeypatch)
    add_worker(runtime, docker, "main")
    add_worker(runtime, docker, "paused", "paused")
    docker.fail_logs = True
    with pytest.raises(RuntimeError, match="archive"):
        runtime.resume_sessions({"original-token": "main"}, "main")
    assert docker.states["main"]["Status"] == "exited"
    assert docker.states["paused"]["Status"] == "paused"

    def unavailable(*args, **kwargs):
        raise RuntimeError("Docker daemon unavailable")

    monkeypatch.setattr(runtime, "docker", unavailable)
    with pytest.raises(RuntimeError, match="daemon"):
        runtime.resume_sessions({"original-token": "main"}, "main")
    assert docker.states["paused"]["Status"] == "paused"
