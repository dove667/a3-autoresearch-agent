"""Local semantic memory is authenticated and reclaimed with its owning run."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from a3_research_agent.host.memory import lifecycle, model
from a3_research_agent.host.memory.embedding import LlamaEmbedder
from a3_research_agent.host.memory.worker import create_memory_app, embedding_command


def test_model_verification_rejects_partial_and_changed_downloads(tmp_path, monkeypatch):
    path = tmp_path / "model.gguf"
    path.write_bytes(b"bad")
    monkeypatch.setattr(model, "MODEL_BYTES", 3)
    with pytest.raises(ValueError, match="SHA256"):
        model.verify_model(path)
    path.write_bytes(b"partial")
    with pytest.raises(ValueError, match="size"):
        model.verify_model(path)


def test_memory_service_requires_credential_and_keeps_run_scoped_notes(tmp_path):
    app = create_memory_app(tmp_path, "today", "private-test-token", None)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 401
        assert client.post("/memory", json={"action": "list"}).status_code == 401
        client.headers["Authorization"] = "Bearer private-test-token"
        assert client.get("/health").json()["run_id"] == "today"
        entry = client.post(
            "/memory",
            json={
                "action": "write",
                "content": "Check source manifest before training",
                "lifetime": "run",
            },
        ).json()
        assert entry["run_ids"] == ["today"]
        hits = client.post("/memory", json={"action": "search", "key": "manifest"}).json()
        assert len(hits["items"]) == 1
        assert client.post("/memory", content="x" * 65537).status_code == 413
        assert client.post("/memory", content=iter([b"x" * 40000, b"x" * 40000])).status_code == 413
        assert (
            client.post("/memory", content="{}", headers={"content-length": "bad"}).status_code
            == 400
        )


def test_readiness_failure_reclaims_worker_group_and_log(tmp_path, monkeypatch):
    run = tmp_path / "trajectories/test"
    run.mkdir(parents=True)
    process = Mock(pid=1234)
    process.wait.return_value = 0
    monkeypatch.setattr(lifecycle, "verify_model", lambda path: path)
    monkeypatch.setattr(lifecycle.shutil, "which", lambda name: "/test/llama-server")
    monkeypatch.setattr(lifecycle.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(lifecycle, "wait_ready", Mock(side_effect=TimeoutError("not ready")))
    monkeypatch.setattr(lifecycle, "group_exists", lambda pid: False)
    killed = []
    monkeypatch.setattr(lifecycle.os, "killpg", lambda pid, sig: killed.append(pid))
    service = lifecycle.MemoryService(tmp_path, run, "test")
    with pytest.raises(TimeoutError), service:
        pytest.fail("A failed service must never enter the research body")
    assert killed == [1234]
    assert service.process is None and service.log is None


def test_query_instruction_is_applied_only_to_query(monkeypatch):
    embedder = LlamaEmbedder("http://127.0.0.1:1", "token")
    texts = []
    monkeypatch.setattr(
        LlamaEmbedder, "__call__", lambda self, batch: texts.extend(batch) or [[1.0]]
    )
    assert embedder.query("避免重复实验") == [1.0]
    assert texts[0].endswith("Query: 避免重复实验")
    assert texts[0].startswith("Instruct: ")


def test_embedding_process_is_private_and_has_small_explicit_resource_limits(tmp_path):
    command = embedding_command("llama-server", tmp_path / "model", 1234)
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--parallel") + 1] == "1"
    assert command[command.index("--pooling") + 1] == "last"
    assert "--embedding" in command
    assert not any("token" in argument for argument in command)


def test_shutdown_kills_surviving_child_after_worker_exits(tmp_path, monkeypatch):
    import os
    import signal
    import subprocess
    import sys
    import time

    ready = tmp_path / "child-pid"
    child_script = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
    parent_script = (
        "import subprocess,sys,time;from pathlib import Path;"
        "p=subprocess.Popen([sys.executable,'-c',sys.argv[2]]);"
        "Path(sys.argv[1]).write_text(str(p.pid));time.sleep(.2)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", parent_script, str(ready), child_script], start_new_session=True
    )
    try:
        process.wait(timeout=5)
        child_pid = int(ready.read_text())
        run = tmp_path / "trajectories/test"
        run.mkdir(parents=True)
        service = lifecycle.MemoryService(tmp_path, run, "test")
        service.process = process
        monkeypatch.setattr(lifecycle, "SHUTDOWN_GRACE_SECONDS", 0.2)
        service.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = subprocess.run(
                ["ps", "-p", str(child_pid), "-o", "stat="],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(0.05)
        assert not state or state.startswith("Z")
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("rows", ["", "1234 Z\n", "9999 S\n1234 Z+\n"])
def test_group_permission_error_with_no_live_members_is_closed(monkeypatch, rows):
    monkeypatch.setattr(lifecycle.os, "killpg", Mock(side_effect=PermissionError("vanished group")))
    monkeypatch.setattr(
        lifecycle.subprocess, "run", lambda *args, **kwargs: Mock(returncode=0, stdout=rows)
    )
    assert lifecycle.group_exists(1234) is False
    lifecycle.signal_group(1234, 15)


def test_group_permission_error_never_hides_a_live_member(monkeypatch):
    monkeypatch.setattr(lifecycle.os, "killpg", Mock(side_effect=PermissionError("live group")))
    monkeypatch.setattr(
        lifecycle.subprocess, "run", lambda *args, **kwargs: Mock(returncode=0, stdout="1234 S\n")
    )
    assert lifecycle.group_exists(1234) is True
    with pytest.raises(PermissionError):
        lifecycle.signal_group(1234, 15)
