"""Human CLI validation before operational Host launch."""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from a3_research_agent.harness import state_paths
from a3_research_agent.host.inputs.versions import InputVersions
from a3_research_agent.host.launch import launch_run
from a3_research_agent.host.models.settings import model_settings
from a3_research_agent.host.remote_client import remote_call, validate_remote_health
from a3_research_agent.host.runs.continuation import resolve_harness_start
from a3_research_agent.host.runs.directories import create_run_directory
from a3_research_agent.host.runs.state import RunLease
from a3_research_agent.host.source_snapshots import git
from a3_research_agent.project import checkout_root, resolve_project_root


def launch_main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--night-window", action="store_true", help="Resume only from 21:00 to 09:00 Beijing time"
    )
    parser.add_argument(
        "--check", action="store_true", help="Validate deployment without starting an Agent"
    )
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="Continue saved context with current versioned Agent inputs",
    )
    parser.add_argument(
        "--run-harness",
        type=Path,
        help="Start research from an explicitly selected published run/package directory",
    )
    parser.add_argument(
        "--harness-commit",
        help="Start fresh from a full published origin/main Harness commit",
    )
    parser.add_argument(
        "--feedback-file",
        type=Path,
        help="On resume, explicitly add a human feedback file before the next main model call",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        help="Infrastructure project directory (required outside a source checkout)",
    )
    args = parser.parse_args()
    try:
        root = resolve_project_root(args.project_root or checkout_root())
    except ValueError as error:
        parser.error(str(error))
    if sum(bool(value) for value in (args.resume_from, args.run_harness, args.harness_commit)) > 1:
        parser.error("Select only one of --harness-commit, --resume-from or --run-harness")
    if not (args.resume_from or args.run_harness or args.harness_commit or args.check):
        parser.error("Select --harness-commit <published SHA> or --resume-from <published run>")
    if args.feedback_file and not args.resume_from:
        parser.error("--feedback-file requires --resume-from")
    args.input_proposal = None
    try:
        from a3_research_agent.host.published_harness import (
            COMMAND,
            native_source,
            project_for_run,
            run_commit,
        )
        from a3_research_agent.host.runs.continuation import HarnessStart

        if args.resume_from and project_for_run(args.resume_from.resolve()) != root:
            raise ValueError("Resume must select a trajectory in the configured project")
        start = resolve_harness_start(args.run_harness) if args.run_harness else None
        if args.harness_commit:
            start = HarnessStart(
                COMMAND.copy(), {state_paths.CHECKPOINT: "[]"}, harness_commit=args.harness_commit
            )
        if (start and not start.harness_commit) or (
            args.resume_from and not run_commit(args.resume_from.resolve())
        ):
            raise ValueError(
                "Generated/cold-start execution is retired; publish reviewed Harness source and select --harness-commit"
            )
        selected = (
            run_commit(args.resume_from.resolve())
            if args.resume_from
            else start.harness_commit
            if start
            else None
        )
        if selected:
            native_source(root, selected)
        if not args.resume_from and not args.check:
            # Freeze the current prompts before deployment checks or run creation.
            args.input_proposal = InputVersions(root, None).prepare()
    except (ValueError, OSError) as error:
        parser.error(str(error))
    load_dotenv(os.environ.get("A3_RSI_ENV_FILE", root / ".env"), override=True)
    scheduled_run = os.environ.get("A3_NIGHT_WINDOW_RUN_ID")
    night_child = os.environ.get("A3_NIGHT_WINDOW_CHILD") == "1"
    if scheduled_run and args.resume_from and not args.check and not args.night_window:
        from a3_research_agent.cli.night_release import night_state_directory, read_json

        schedule = read_json(
            night_state_directory(root / "trajectories" / scheduled_run) / "night-schedule.json",
            {},
        )
        protected = {scheduled_run, schedule.get("current")}
        if args.resume_from.resolve().name in protected and not night_child:
            parser.error("This run must be resumed with --night-window")
    if night_child:
        from datetime import datetime

        from a3_research_agent.cli.night_window import BEIJING, window_boundary

        if not window_boundary(datetime.now(BEIJING))[0]:
            parser.error("This run can only start from 21:00 to 09:00 Beijing time")
    if args.night_window:
        if not args.resume_from or args.check:
            parser.error("--night-window requires an existing --resume-from run")
        from a3_research_agent.cli.night_release import NightSchedule
        from a3_research_agent.cli.night_window import run_nightly
        from a3_research_agent.host.runs.state import RunStateStore

        anchor = args.resume_from.resolve()
        command = [
            sys.executable,
            "-m",
            "a3_research_agent.cli.launch",
            "--project-root",
            str(root),
            "--resume-from",
            str(anchor),
        ]
        if args.feedback_file:
            command.extend(["--feedback-file", str(args.feedback_file.resolve())])
        schedule = NightSchedule(anchor, command)
        with RunLease(schedule.directory / "night-launcher"):
            state = RunStateStore(schedule.current_run())
            if not state.path.is_file() or state.load().get("status") != "suspended":
                parser.error("--night-window requires a suspended run")
            raise SystemExit(run_nightly(command, state, schedule=schedule))
    required = (
        "MODEL_NAME",
        "A3_PROJECT_ROOT",
        "A3_REMOTE_HOST",
        "A3_REMOTE_CONTROLLER",
        "A3_REMOTE_RUN_ROOT",
    )
    if os.environ.get("A3_REMOTE_BACKEND", "docker") == "process":
        required += ("A3_REMOTE_DATA_ROOT", "A3_REMOTE_PROCESS_PYTHON")
    else:
        required += ("A3_REMOTE_IMAGE",)
    for key in required:
        if not os.environ.get(key):
            raise SystemExit(f"Missing deployment setting: {key}")
    a3 = Path(os.environ["A3_PROJECT_ROOT"]).resolve()
    model_settings(os.environ)
    remote_ready = remote_call("health", {})
    validate_remote_health(remote_ready)
    if args.check:
        print(
            json.dumps(
                {
                    "local_execution": "process",
                    "remote_controller": "reachable",
                    "a3_commit": git(a3, "rev-parse", "HEAD"),
                    "remote_response_received": isinstance(remote_ready, dict),
                }
            )
        )
        return
    run_root = root / "trajectories"
    run = args.resume_from.resolve() if args.resume_from else create_run_directory(run_root)
    with RunLease(root / ".runtime/research-workspace"), RunLease(run):
        launch_run(args, run, a3, start)


def main() -> None:
    launch_main()


if __name__ == "__main__":
    main()
