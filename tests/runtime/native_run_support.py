"""Create current native Host metadata without starting a model or a process."""

import json
from pathlib import Path

from a3_research_agent.host.published_harness import COMMAND, pin_run, research_workspace
from a3_research_agent.host.runs.directories import debug_directory
from a3_research_agent.host.runs.state import RunStateStore
from a3_research_agent.host.sessions.processes import ProcessRuntime

COMMIT = "a" * 40


def native_run(tmp_path: Path, *, name="run-current", actor="main", stopped=False):
    root = tmp_path / "project"
    run = root / "trajectories" / name
    run.mkdir(parents=True)
    pin_run(run, COMMIT)
    workspace = research_workspace(run, actor)
    workspace.mkdir(parents=True)
    session = run / "sessions" / actor
    session.mkdir(parents=True)
    (session / "workspace").symlink_to(workspace, target_is_directory=True)
    inputs, public = run / "inputs", run / "public/a3"
    inputs.mkdir()
    public.mkdir(parents=True)
    source = root / "published-source"
    package = source / "a3_research_agent/harness"
    package.mkdir(parents=True, exist_ok=True)
    for path in (
        source / "a3_research_agent/__init__.py",
        package / "__init__.py",
        package / "paths.py",
        package / "state_paths.py",
        package / "agent.py",
    ):
        path.write_text("")
    (package / "state_paths.py").write_text("LAYOUT_VERSION = 2\n")
    checkpoint = [{"role": "user", "content": "existing research and observations"}]
    (run / "conversation/checkpoint.json").parent.mkdir(parents=True, exist_ok=True)
    (run / "conversation/checkpoint.json").write_text(json.dumps(checkpoint))
    (workspace / "scripts").mkdir()
    (workspace / "experiments").mkdir()
    (workspace / "experiments/lesson.md").write_text(
        '{"id":"lesson","text":"retain the confirmed finding"}\n'
    )
    (workspace / "scripts/train.py").write_text('print("research source")\n')
    (run / "conversation/continuation.json").write_text(
        json.dumps(
            {
                "version": 1,
                "command": COMMAND,
                "files": ["experiments/lesson.md", "scripts/train.py"],
            }
        )
    )
    record = {
        "version": 1,
        "backend": "process",
        "owner": actor,
        "session_id": actor,
        "closed": False,
        "command": COMMAND,
        "launch_pending": False,
        "exit_archived": True,
        "terminal_state": {"status": "exited", "exit_code": 0},
        "self_stop_requested": stopped,
        "self_stopped": stopped,
        "inputs": str(inputs),
        "published_source": str(source),
    }
    (session / "session.json").write_text(json.dumps(record))
    console = debug_directory(run) / actor / "console.log"
    console.parent.mkdir(parents=True)
    console.write_text("saved native process console\n")
    (run / "startup.json").write_text(
        json.dumps({"version": 1, "run_id": run.name, "harness_commit": COMMIT})
    )
    state = {
        "version": 1,
        "backend": "process",
        "run_id": run.name,
        "model": "configured/model",
        "harness_commit": COMMIT,
        "status": "suspended",
        "endpoint": "http://127.0.0.1:34567",
        "gateway": {
            "sessions": {"root-token": run.name, "main-token": actor},
            "root_owners": [run.name],
            "session_records": {
                actor: {
                    "owner": actor,
                    "closed": False,
                    "token": "main-token",
                    "self_stopped": stopped,
                }
            },
            "jobs": {actor: ["owned-job"]},
            "main_owner": actor,
            "fixtures": {},
        },
    }
    store = RunStateStore(run)
    store.save(state)
    runtime = ProcessRuntime(
        root=run / "sessions",
        inputs=inputs,
        public_a3=public,
        endpoint=state["endpoint"],
        published_source=source,
    )
    runtime.load_records()
    return run, workspace, state, store, runtime
