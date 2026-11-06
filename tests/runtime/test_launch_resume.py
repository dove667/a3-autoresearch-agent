import hashlib
import json
import socket
from argparse import Namespace

import pytest

from a3_research_agent.host import bootstrap as launch
from a3_research_agent.host.runs.state import RunStateStore


@pytest.mark.parametrize(
    "phase,task_profile",
    [
        ("phase-one", "standard"),
        ("phase-two", "standard"),
        ("phase-one", "web-extension"),
        ("phase-one", "skills-extension"),
    ],
)
@pytest.mark.parametrize("submitted_builder", [False, True])
def test_in_place_resume_keeps_run_and_can_supervise_after_builder_submission(
    tmp_path, monkeypatch, submitted_builder, phase, task_profile
):
    run = tmp_path / "run-original-timestamp"
    inputs = run / "inputs"
    inputs.mkdir(parents=True)
    files = {
        "goal.md": "original goal",
        "acceptance.md": "## A. original",
        "bootstrap.yaml": "agent: {}\nrun:\n  task: original\n",
    }
    for name, text in files.items():
        (inputs / name).write_text(text)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        endpoint = f"http://host.docker.internal:{probe.getsockname()[1]}"
    (inputs / "deployment.json").write_text(
        json.dumps({"gateway": endpoint, "sessions": {"max_active": 2}})
    )
    startup = {
        "run_id": run.name,
        "phase": phase,
        **({"task_profile": task_profile} if task_profile != "standard" else {}),
        "mode": "rsi",
        "inputs": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()},
    }
    (run / "startup.json").write_text(json.dumps(startup))
    original_startup = (run / "startup.json").read_bytes()
    actor = run.name if submitted_builder else "candidate-main"
    if submitted_builder:
        (run / "trajectory.json").write_text(json.dumps({"info": {"exit_status": "Submitted"}}))
    store = RunStateStore(run)
    store.save(
        {
            "run_id": run.name,
            "model": "test/model",
            "model_class": "test_adapter",
            "status": "suspended",
            "endpoint": endpoint,
            "gateway": {
                "sessions": {"root-token": run.name},
                "root_owners": [run.name],
                "children": {},
                "scope_parents": {},
                "jobs": {},
                "fixtures": {},
                "evolution": {"main_owner": actor, "trials": {}, "transitions": []},
            },
        }
    )
    events = []

    class Runtime:
        def __init__(self, **kwargs):
            self.records = {}

        def load_records(self):
            events.append("load")

        def resume_sessions(self, credentials, main):
            events.append(("resume", main))

        def suspend(self):
            events.append("suspend")

        def close(self):
            events.append("close")

    class Supervisor:
        def __init__(self, **kwargs):
            pass

        def run(self, *, initial_builder):
            assert not initial_builder
            return {"status": "incomplete", "builder_error": "example"}

    monkeypatch.setenv("A3_WEB_ENABLED", "0")
    monkeypatch.setenv("MODEL_NAME", "test/model")
    monkeypatch.setenv("MODEL_CLASS", "test_adapter")
    monkeypatch.setattr(launch, "CandidateRuntime", Runtime)
    monkeypatch.setattr(launch, "RunSupervisor", Supervisor)
    launch.launch_run(
        Namespace(resume_from=run, verify_deployment=False), run, tmp_path, "image", None
    )
    assert (run / "startup.json").read_bytes() == original_startup
    assert json.loads((inputs / "input-manifest.json").read_text())["phase"] == phase
    assert json.loads((inputs / "input-manifest.json").read_text())["task_profile"] == task_profile
    if task_profile in ("web-extension", "skills-extension"):
        assert json.loads((inputs / "input-manifest.json").read_text())["acceptance_checks"] == [
            "F" if task_profile == "skills-extension" else "E"
        ]
    assert store.load()["run_id"] == run.name
    assert store.load()["status"] == "suspended"
    assert ("resume", actor) in events
    assert "suspend" in events and "close" not in events
    assert [p.name for p in tmp_path.iterdir() if p.is_dir()] == [run.name]
    assert json.loads((inputs / "deployment.json").read_text())["sessions"]["max_active"] == 2
    web = json.loads((inputs / "deployment.json").read_text())["web"]
    assert web["enabled"] is False
    assert web["budget"]["search"] == {"limit": None, "used": 0}
    assert web["no_run_cap"] is True
    assert "api_key" not in json.dumps(web)
