"""Human CLI validation before operational Host launch."""

import argparse
import json
import os
import subprocess
from pathlib import Path


def launch_main(runtime) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Validate deployment without starting an Agent"
    )
    parser.add_argument(
        "--verify-deployment", action="store_true", help="Run a bounded agent readiness check"
    )
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="Continue saved context with current versioned Agent inputs",
    )
    parser.add_argument(
        "--run-harness",
        type=Path,
        help="Start from the retained result of an explicitly selected run/package directory",
    )
    parser.add_argument(
        "--phase",
        choices=runtime.PHASES,
        help="Select stage inputs; new runs default to phase-one, resumes keep their stage",
    )
    parser.add_argument(
        "--task-profile",
        choices=runtime.TASK_PROFILES,
        help="New tasks default to standard; resumes keep their recorded profile",
    )
    parser.add_argument(
        "--feedback-file",
        type=Path,
        help="On resume, explicitly add a human feedback file before the next main model call",
    )
    parser.add_argument(
        "--initial-feedback-file",
        type=Path,
        help="Approved initial user input for a new retained phase-one extension task",
    )
    parser.add_argument(
        "--agent-inputs-dir",
        type=Path,
        help="Complete custom Agent input directory for a new --run-harness task",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        help="Infrastructure project directory (required outside a source checkout)",
    )
    args = parser.parse_args()
    try:
        runtime.ROOT = runtime.resolve_project_root(args.project_root or runtime.ROOT)
    except ValueError as error:
        parser.error(str(error))
    if args.agent_inputs_dir and (
        not args.run_harness
        or args.resume_from
        or args.check
        or args.verify_deployment
        or args.task_profile not in (None, "standard")
        or args.initial_feedback_file
    ):
        parser.error(
            "--agent-inputs-dir requires a new --run-harness with no preset profile; put initial-feedback.md in the directory"
        )
    args.custom_input_proposal = None
    if args.feedback_file and not args.resume_from:
        parser.error("--feedback-file requires --resume-from")
    if args.run_harness and (args.resume_from or args.verify_deployment):
        parser.error("--run-harness cannot be combined with --resume-from or --verify-deployment")
    try:
        args.phase = runtime.resolve_phase(args.resume_from, args.phase)
        args.task_profile = runtime.resolve_task_profile(args.resume_from, args.task_profile)
    except ValueError as error:
        parser.error(str(error))
    if args.initial_feedback_file and (
        not args.run_harness
        or args.resume_from
        or args.check
        or args.verify_deployment
        or args.phase != "phase-one"
        or args.task_profile not in runtime.EXTENSION_PROFILES
    ):
        parser.error("--initial-feedback-file requires a new phase-one extension --run-harness")
    if args.phase == "phase-two" and not (args.run_harness or args.resume_from):
        parser.error("phase-two requires --run-harness or a phase-two --resume-from")
    if args.phase == "phase-two" and args.verify_deployment:
        parser.error("--verify-deployment is a phase-one readiness check")
    if args.task_profile in runtime.EXTENSION_PROFILES:
        if args.phase != "phase-one" or args.verify_deployment:
            parser.error(
                f"{args.task_profile} requires phase-one and is not a deployment readiness check"
            )
        if not (args.run_harness or args.resume_from):
            parser.error(f"{args.task_profile} requires --run-harness or a matching --resume-from")
        if args.resume_from and not (args.resume_from / "startup.json").is_file():
            parser.error(f"{args.task_profile} resume requires an existing recorded run")
        if args.run_harness and not (args.run_harness / "retained.json").is_file():
            parser.error(f"{args.task_profile} requires an existing retained run source")
    args.initial_feedback = None
    try:
        if args.initial_feedback_file:
            with args.initial_feedback_file.open("rb") as stream:
                args.initial_feedback = runtime.HumanFeedback.validate_content(
                    stream.read(runtime.MAX_FEEDBACK_BYTES + 1)
                )
        if args.agent_inputs_dir:
            # Pure validation/snapshot before environment, Docker, SSH or run creation.
            args.custom_input_proposal = runtime.InputVersions(
                runtime.ROOT,
                None,
                phase=args.phase,
                custom_dir=args.agent_inputs_dir,
            ).prepare()
            args.initial_feedback = args.custom_input_proposal["contents"].get(
                "initial-feedback.md"
            )
        start = runtime.resolve_harness_start(args.run_harness) if args.run_harness else None
        if args.custom_input_proposal:
            runtime.prepare_custom_task(
                start, args.custom_input_proposal["contents"]["goal.md"], args.initial_feedback
            )
        if args.task_profile in runtime.EXTENSION_PROFILES and start:
            # Validate source and checkpoint before deployment or resource access.
            runtime.prepare_task_start(
                start,
                args.phase,
                args.task_profile,
                "",
                initial_feedback=args.initial_feedback,
                initial_feedback_source=args.initial_feedback_file,
            )
    except (ValueError, OSError) as error:
        parser.error(str(error))
    runtime.load_dotenv(os.environ.get("A3_RSI_ENV_FILE", runtime.ROOT / ".env"), override=True)
    required = (
        "MODEL_NAME",
        "MODEL_CLASS",
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
    runtime.get_model_class(os.environ["MODEL_NAME"], os.environ["MODEL_CLASS"])
    image = os.environ.get("A3_LOCAL_IMAGE", "a3-agent-bootstrap:local")
    subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, check=True)
    remote_ready = runtime.remote_call("health", {})
    runtime.validate_remote_health(remote_ready)
    if args.check:
        print(
            json.dumps(
                {
                    "local_image": "available",
                    "remote_controller": "reachable",
                    "a3_commit": runtime.git(a3, "rev-parse", "HEAD"),
                    "remote_response_received": isinstance(remote_ready, dict),
                }
            )
        )
        return
    run_root = runtime.ROOT / "artifacts"
    run_root /= "validation/deployment" if args.verify_deployment else "rsi"
    run = args.resume_from.resolve() if args.resume_from else runtime.create_run_directory(run_root)
    with runtime.RunLease(run):
        runtime.launch_run(args, run, a3, image, start)


def main() -> None:
    from a3_research_agent.host import bootstrap

    launch_main(bootstrap)


if __name__ == "__main__":
    main()
