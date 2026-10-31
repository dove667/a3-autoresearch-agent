import json

import pytest

from runtime.local.candidate_runtime import CandidateContainerMissingError, CandidateRuntime


def test_paused_sessions_reconnect_without_reexecuting_or_changing_identity(tmp_path, monkeypatch):
    runtime = CandidateRuntime(
        root=tmp_path / "candidates",
        image="image",
        inputs=tmp_path,
        public_a3=tmp_path,
        endpoint="http://original:1234",
    )
    record = {"owner": "main", "session_id": "main", "container_id": "docker-1", "closed": False}
    directory = runtime.root / "main"
    directory.mkdir()
    (directory / "session.json").write_text(json.dumps(record))
    commands = []
    monkeypatch.setattr(runtime, "docker", lambda *args, **kwargs: commands.append(args) or "")
    monkeypatch.setattr(runtime, "state", lambda record: {"Status": "paused"})
    runtime.load_records()
    runtime.resume_sessions({"secret": "main"}, "main")
    assert commands == [("unpause", "docker-1")]
    assert runtime.records["main"]["owner"] == "main"


def test_interruption_pauses_containers_without_cancelling_them(tmp_path, monkeypatch):
    runtime = CandidateRuntime(
        root=tmp_path,
        image="image",
        inputs=tmp_path,
        public_a3=tmp_path,
        endpoint="http://original",
    )
    record = {"owner": "main", "container_id": "docker-1", "closed": False}
    runtime.records["main"] = record
    (tmp_path / "main").mkdir()
    monkeypatch.setattr(runtime, "state", lambda record: {"Status": "running"})
    commands = []
    monkeypatch.setattr(runtime, "docker", lambda *args, **kwargs: commands.append(args) or "")
    runtime.suspend()
    assert commands == [("pause", "docker-1")]
    assert not record["closed"]


@pytest.mark.parametrize(
    "checkpoint",
    ['[{"role":"user","content":"prior context"}]', "7", "[1,2,3]", '{"messages":null}'],
)
def test_missing_main_restarts_at_checkpoint_in_original_workspace(
    tmp_path, monkeypatch, checkpoint
):
    runtime = CandidateRuntime(
        root=tmp_path / "candidates",
        image="image",
        inputs=tmp_path,
        public_a3=tmp_path,
        endpoint="http://original:1234",
    )
    directory = runtime.root / "main"
    workspace = directory / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "entry.py").write_text("pass")
    (workspace / "checkpoint.json").write_text(checkpoint)
    (workspace / "memory.txt").write_text("keep all workspace files")
    (workspace / "resume.json").write_text(
        json.dumps(
            {
                "command": ["python", "entry.py", "--resume"],
                "files": ["entry.py", "checkpoint.json"],
            }
        )
    )
    runtime.records["main"] = {"owner": "main", "container_id": "lost", "closed": False}
    monkeypatch.setattr(
        runtime, "state", lambda record: (_ for _ in ()).throw(CandidateContainerMissingError())
    )
    created = []
    monkeypatch.setattr(runtime, "create", lambda *args, **kwargs: created.append((args, kwargs)))
    runtime.resume_sessions({"same-token": "main"}, "main")
    arguments, options = created[0]
    assert arguments == ("main", ["python", "entry.py", "--resume"], {}, "same-token")
    assert options["workspace"] == workspace.resolve()
    assert options["reuse"] is True
    assert (workspace / "memory.txt").read_text() == "keep all workspace files"


def test_suspend_continues_after_one_container_fails(tmp_path, monkeypatch):
    import pytest

    runtime = CandidateRuntime(
        root=tmp_path,
        image="image",
        inputs=tmp_path,
        public_a3=tmp_path,
        endpoint="http://original",
    )
    for name in ["missing", "live"]:
        (tmp_path / name).mkdir()
        runtime.records[name] = {"owner": name, "container_id": name, "closed": False}

    def state(record):
        if record["owner"] == "missing":
            raise RuntimeError("missing")
        return {"Status": "running"}

    commands = []
    monkeypatch.setattr(runtime, "state", state)
    monkeypatch.setattr(runtime, "docker", lambda *args, **kwargs: commands.append(args) or "")
    with pytest.raises(RuntimeError, match="missing"):
        runtime.suspend()
    assert commands == [("pause", "live")]
    assert (tmp_path / "live/session.json").exists()


def test_pending_tool_call_is_not_replayed_after_container_loss(tmp_path, monkeypatch):
    import pytest

    runtime = CandidateRuntime(
        root=tmp_path / "candidates",
        image="image",
        inputs=tmp_path,
        public_a3=tmp_path,
        endpoint="http://original",
    )
    workspace = runtime.root / "main/workspace"
    workspace.mkdir(parents=True)
    (workspace / "entry.py").write_text("pass")
    (workspace / "checkpoint.json").write_text(
        json.dumps(
            [
                {"type": "function_call", "name": "gateway", "call_id": "pending"},
            ]
        )
    )
    (workspace / "resume.json").write_text(
        json.dumps(
            {
                "command": ["python", "entry.py", "--resume"],
                "files": ["entry.py", "checkpoint.json"],
            }
        )
    )
    runtime.records["main"] = {"owner": "main", "container_id": "lost", "closed": False}
    monkeypatch.setattr(
        runtime, "state", lambda record: (_ for _ in ()).throw(CandidateContainerMissingError())
    )
    with pytest.raises(ValueError, match="Unresolved tool"):
        runtime.resume_sessions({"same-token": "main"}, "main")
