"""Launch a cold start, resume a run, or continue a retained Harness."""

import argparse
import hashlib
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
import litellm
import uvicorn
import yaml
from dotenv import load_dotenv
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.models import get_model, get_model_class

from runtime.local.candidate_runtime import MAX_ACTIVE_CANDIDATES, CandidateRuntime
from runtime.local.candidate_versions import CandidateVersions
from runtime.local.context_resume import ContextAgent, load_context
from runtime.local.continuation import (
    import_retained_exports,
    prepare_fresh_task,
    publish_run_result,
    resolve_harness_start,
)
from runtime.local.host_gateway import create_app
from runtime.local.human_feedback import HumanFeedback
from runtime.local.input_versions import PHASES, InputVersions, resolve_phase
from runtime.local.isolation_fixture import IsolationFixtures
from runtime.local.recovery import stage_recovery
from runtime.local.run_resume import load_resume_state
from runtime.local.run_state import RunLease, RunStateStore
from runtime.local.run_supervisor import RunSupervisor

ROOT = Path(__file__).resolve().parents[2]


def create_run_directory(root: Path) -> Path:
    """Reserve a directory named with the Host's local start time."""
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("run-%Y%m%d-%H%M%S-%f")
    sequence = 1
    while True:
        name = timestamp if sequence == 1 else f"{timestamp}-{sequence}"
        run = root / name
        try:
            run.mkdir(mode=0o700)
        except FileExistsError:
            sequence += 1
        else:
            return run


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def snapshot_checkout(source: Path, destination: Path) -> None:
    subprocess.run(
        ["git", "clone", "--quiet", "--no-local", str(source), str(destination)], check=True
    )
    report_checkout = destination / "reports"
    if report_checkout.is_dir():
        shutil.rmtree(report_checkout)
    files = (
        subprocess.check_output(
            [
                "git",
                "-C",
                str(source),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ]
        )
        .decode()
        .split("\0")
    )
    for name in filter(None, files):
        if Path(name).parts[0] == "reports":
            continue
        src, dst = source / name, destination / name
        if not src.exists() and dst.is_file():
            dst.unlink()
        if src.is_file() and not src.is_symlink():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    git(destination, "config", "user.name", "Research Agent")
    git(destination, "config", "user.email", "agent@localhost")
    # A submodule's host .git indirection must never reach into the live checkout.
    mini = source / "third_party/mini-swe-agent"
    if mini.is_dir():
        target = destination / "third_party/mini-swe-agent"
        target.rmdir()
        snapshot_checkout(mini, target)
    # The selected files and exclusions form this isolated checkout's starting point.
    if git(destination, "status", "--porcelain"):
        git(destination, "add", "--all")
        git(
            destination,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "chore: establish isolated execution baseline",
        )


def remote_call(operation: str, payload: dict) -> dict:
    arguments = [
        os.environ.get("A3_REMOTE_PYTHON", "python3"),
        os.environ["A3_REMOTE_CONTROLLER"],
        "--root",
        os.environ["A3_REMOTE_RUN_ROOT"],
        "--image",
        os.environ["A3_REMOTE_IMAGE"],
    ]
    for environment, flag in (
        ("A3_REMOTE_MAX_JOBS", "--max-jobs"),
        ("A3_REMOTE_MEMORY", "--memory"),
        ("A3_REMOTE_MAX_GPUS", "--max-gpus"),
        ("A3_REMOTE_GPU_MEMORY_MB", "--gpu-memory-mb"),
        ("A3_REMOTE_GPU_HEADROOM_MB", "--gpu-headroom-mb"),
        ("A3_REMOTE_GPU_CAPACITY_MB", "--gpu-capacity-mb"),
        ("A3_REMOTE_JOB_TIMEOUT", "--timeout"),
        ("A3_REMOTE_RUN_STORAGE_BYTES", "--run-storage-bytes"),
        ("A3_REMOTE_MODELS_ROOT", "--models-root"),
        ("A3_REMOTE_MODEL_CACHE_ROOT", "--model-cache-root"),
        ("A3_REMOTE_DATASETS_ROOT", "--datasets-root"),
    ):
        if os.environ.get(environment):
            arguments += [flag, os.environ[environment]]
    if os.environ.get("A3_REMOTE_SHARE_GPUS") == "1":
        arguments += ["--share-gpus"]
    if os.environ.get("A3_REMOTE_PRESERVE_MODELS_PATH") == "1":
        arguments += ["--preserve-models-path"]
    command = shlex.join([*arguments, operation])
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            os.environ["A3_REMOTE_HOST"],
            command,
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )
    return json.loads(result.stdout)


def prepare_workspace(workspace: Path, a3: Path, harness: Path = ROOT) -> None:
    snapshot_checkout(a3, workspace / "a3")
    snapshot_checkout(harness, workspace / "harness")
    (workspace / "run").mkdir()
    (workspace / "harness/AGENTS.md").write_text(
        (ROOT / "configs/prompts/workspace.md").read_text()
    )


def bootstrap_source(parent: Path | None) -> Path:
    if parent and (parent / "workspace/harness/.git").exists():
        return parent / "workspace/harness"
    return ROOT


def input_hashes(inputs: Path) -> dict[str, str]:
    names = (
        "goal.md",
        "acceptance.md",
        "bootstrap.yaml",
        "execution.md",
        "phase-instructions.md",
        "protocol.md",
    )
    return {
        name: hashlib.sha256((inputs / name).read_bytes()).hexdigest()
        for name in names
        if (inputs / name).is_file()
    }


def local_storage_bytes(run: Path) -> int:
    """Count this run's files without following links into source or other runs."""
    total = 0
    for directory, names, files in os.walk(run, followlinks=False):
        names[:] = [name for name in names if not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            try:
                if not path.is_symlink() and path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass
    return total


def remote_limits() -> dict:
    return {
        "cpus": 2,
        "memory": os.environ.get("A3_REMOTE_MEMORY", "8g"),
        "max_jobs": int(os.environ.get("A3_REMOTE_MAX_JOBS", "2")),
        "timeout_seconds": int(os.environ.get("A3_REMOTE_JOB_TIMEOUT", "900")),
        "run_storage_bytes": int(os.environ.get("A3_REMOTE_RUN_STORAGE_BYTES", "0")),
        "gpu_mode": "memory_shared"
        if os.environ.get("A3_REMOTE_SHARE_GPUS") == "1"
        else "exclusive",
        "max_gpus": int(os.environ.get("A3_REMOTE_MAX_GPUS", "4")),
        "gpu_memory_mb": int(os.environ.get("A3_REMOTE_GPU_MEMORY_MB", "512")),
        "gpu_headroom_mb": int(os.environ.get("A3_REMOTE_GPU_HEADROOM_MB", "2048")),
        "gpu_capacity_mb": int(os.environ.get("A3_REMOTE_GPU_CAPACITY_MB", "0")),
        "storage_scope": "local run files plus new remote data and outputs across all identities in this run",
    }


def validate_remote_health(response: dict) -> None:
    if response.get("status") != "ok" or response.get("protocol_version") != 1:
        raise RuntimeError("Remote controller health or protocol check failed")


def supervised_sessions(client: httpx.Client) -> list[dict]:
    response = client.get("/sessions", params={"include_status": "true"})
    response.raise_for_status()
    return response.json()["sessions"]


def main() -> None:
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
        choices=PHASES,
        help="Select stage inputs; new runs default to phase-one, resumes keep their stage",
    )
    parser.add_argument(
        "--feedback-file",
        type=Path,
        help="On resume, explicitly add a human feedback file before the next main model call",
    )
    args = parser.parse_args()
    if args.feedback_file and not args.resume_from:
        parser.error("--feedback-file requires --resume-from")
    if args.run_harness and (args.resume_from or args.verify_deployment):
        parser.error("--run-harness cannot be combined with --resume-from or --verify-deployment")
    try:
        args.phase = resolve_phase(args.resume_from, args.phase)
    except ValueError as error:
        parser.error(str(error))
    if args.phase == "phase-two" and not (args.run_harness or args.resume_from):
        parser.error("phase-two requires --run-harness or a phase-two --resume-from")
    if args.phase == "phase-two" and args.verify_deployment:
        parser.error("--verify-deployment is a phase-one readiness check")
    start = resolve_harness_start(args.run_harness) if args.run_harness else None
    load_dotenv(os.environ.get("A3_RSI_ENV_FILE", ROOT / ".env"), override=True)
    required = (
        "MODEL_NAME",
        "MODEL_CLASS",
        "A3_PROJECT_ROOT",
        "A3_REMOTE_HOST",
        "A3_REMOTE_CONTROLLER",
        "A3_REMOTE_RUN_ROOT",
        "A3_REMOTE_IMAGE",
    )
    for key in required:
        if not os.environ.get(key):
            raise SystemExit(f"Missing deployment setting: {key}")
    a3 = Path(os.environ["A3_PROJECT_ROOT"]).resolve()
    get_model_class(os.environ["MODEL_NAME"], os.environ["MODEL_CLASS"])
    image = os.environ.get("A3_LOCAL_IMAGE", "a3-agent-bootstrap:local")
    subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, check=True)
    remote_ready = remote_call("health", {})
    validate_remote_health(remote_ready)
    if args.check:
        print(
            json.dumps(
                {
                    "local_image": "available",
                    "remote_controller": "reachable",
                    "a3_commit": git(a3, "rev-parse", "HEAD"),
                    "remote_response_received": isinstance(remote_ready, dict),
                }
            )
        )
        return
    run_root = ROOT / "artifacts"
    run_root /= "validation/deployment" if args.verify_deployment else "rsi"
    run = args.resume_from.resolve() if args.resume_from else create_run_directory(run_root)
    with RunLease(run):
        launch_run(args, run, a3, image, start)


def launch_run(args, run: Path, a3: Path, image: str, start) -> None:
    package = (start.command, start.files) if start else None
    kind = (
        "deployment-checks" if args.verify_deployment else "retained-harness" if package else "rsi"
    )
    resuming = args.resume_from is not None
    store = RunStateStore(run)
    saved = None
    resume_context = None
    input_versions = InputVersions(ROOT, run, phase=getattr(args, "phase", None))
    proposed_inputs = input_versions.prepare()
    if resuming:
        startup = json.loads((run / "startup.json").read_text())
        saved = load_resume_state(run, store, os.environ["MODEL_NAME"], os.environ["MODEL_CLASS"])
        actor = saved["gateway"]["evolution"]["main_owner"]
        if actor == run.name:
            checkpoint = json.loads((run / "trajectory.json").read_text())
            if checkpoint.get("info", {}).get("exit_status") != "Submitted":
                resume_context = load_context(run / "trajectory.json")
    run_id = run.name
    inputs, workspace = run / "inputs", run / "workspace"
    private_inputs = run / "private-inputs" if (run / "private-inputs").is_dir() else None
    parent = None
    if not resuming:
        inputs.mkdir(parents=True, mode=0o700)
        workspace.mkdir()
        parent = None
        prepare_workspace(
            workspace,
            parent / "workspace/a3" if parent and not package else a3,
            bootstrap_source(parent),
        )
        if parent and not package:
            shutil.copytree(parent / "workspace/run", workspace / "run", dirs_exist_ok=True)
        private_inputs = None
        if start and start.previous_run:
            private_inputs = run / "private-inputs"
            private_inputs.mkdir()
            stage_recovery(start.previous_run, private_inputs / "history")
            (private_inputs / "previous-run.json").write_text(
                json.dumps(
                    {
                        "run_id": start.previous_run.name,
                        "source_version": start.source_version,
                        "history_index": "/run/input/history/index.json",
                        "goals": "Frozen goal and acceptance are authoritative; historical evidence does not replace them",
                    },
                    indent=2,
                )
            )
        public_a3 = run / "public/a3"
        public_a3.parent.mkdir()
        snapshot_checkout(a3, public_a3)
    input_versions.activate(proposed_inputs, "resume" if resuming else "start")
    task_context = None
    if package and input_versions.phase == "phase-two":
        start, task_context = prepare_fresh_task(start, input_versions.contents["goal.md"])
        package = (start.command, start.files)
    public_a3 = run / "public/a3"
    if saved:
        endpoint = saved["endpoint"]
        port = int(endpoint.rsplit(":", 1)[1])
        token = next(key for key, value in saved["gateway"]["sessions"].items() if value == run_id)
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    else:
        token = secrets.token_urlsafe(32)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        endpoint = f"http://host.docker.internal:{port}"
    store.update(
        run_id=run_id,
        model=os.environ["MODEL_NAME"],
        model_class=os.environ["MODEL_CLASS"],
        endpoint=endpoint,
        status="starting",
        operator_token=secrets.token_urlsafe(32),
    )
    versions = CandidateVersions(run / "versions")
    sandbox = CandidateRuntime(
        root=run / "candidates",
        image=image,
        inputs=inputs,
        public_a3=public_a3,
        endpoint=endpoint,
        versions=versions,
    )
    if resuming:
        sandbox.load_records()
        initial_session = startup.get("session_id")
        if (
            saved.get("imported_host_metadata")
            and private_inputs
            and initial_session in sandbox.records
        ):
            sandbox.records[initial_session]["inputs"] = str(private_inputs)
    phase_instructions = input_versions.model_instructions()[1]
    if args.verify_deployment:
        phase_instructions = ""  # Readiness checks are test sessions, not main Agents.

    def remote_for_run(operation, payload):
        if operation == "submit":
            payload = {**payload, "run_id": run_id}
        return remote_call(operation, payload)

    def report_storage():
        return remote_for_run(
            "usage",
            {
                "run_id": run_id,
                "host_storage_bytes": local_storage_bytes(run),
            },
        )

    fixtures = IsolationFixtures(remote_for_run, run / "fixtures")
    initial_exports = import_retained_exports(start, run_id, remote_for_run)
    human_feedback = HumanFeedback(run)
    if getattr(args, "feedback_file", None):
        if actor == run_id:
            raise ValueError("Human feedback currently requires a generated Harness main session")
        registration = human_feedback.register(args.feedback_file, actor)
        print("Human feedback registered: " + json.dumps(registration), flush=True)
    app = create_app(
        model_name=os.environ["MODEL_NAME"],
        sessions={token: run_id},
        forward_model=litellm.responses,
        remote_call=remote_for_run,
        usage_log=run / "model-usage.jsonl",
        sandbox=sandbox,
        fixtures=fixtures,
        versions=versions,
        initial_inputs=private_inputs,
        main_owner=saved["gateway"]["evolution"]["main_owner"] if saved else run_id,
        start_main_session=bool(package),
        phase_instructions=phase_instructions,
        human_feedback=human_feedback,
        state_store=store,
        restored_state=saved["gateway"] if saved else None,
        input_versions=None if args.verify_deployment else input_versions,
        operator_token=store.load()["operator_token"],
        resource_monitor=report_storage
        if int(os.environ.get("A3_REMOTE_RUN_STORAGE_BYTES", "0"))
        else None,
    )
    app.state.save_checkpoint()
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("Gateway failed to start")
    if not resuming:
        (inputs / "deployment.json").write_text(
            json.dumps(
                {
                    "gateway": endpoint,
                    "workspace": "/workspace",
                    "jobs": "POST /jobs {command: [...], gpu: bool, files: {relative_path: text}}",
                    "status": "GET /jobs/{job_id}",
                    "cancel": "DELETE /jobs/{job_id}",
                    "artifact": "GET /jobs/{job_id}/artifacts?path=relative_path",
                    "authorization": "Bearer $A3_GATEWAY_TOKEN",
                    "model": "openai/sandbox-model",
                    "model_class": "litellm_response",
                    "cost_tracking": "ignore_errors",
                    "usage_accounting": "host gateway model-usage.jsonl; alias prices are unknown",
                    "model_api_base": endpoint + "/v1",
                    "model_api_key": "$A3_GATEWAY_TOKEN",
                    "remote_a3": "/opt/a3",
                    "remote_workspace": "/workspace",
                    "remote_outputs": "/outputs (persistent within the same worker identity)",
                    "remote_models": "/models (read-only, when configured)",
                    "remote_model_cache": "/model-cache (read-only, when configured)",
                    "remote_datasets": "/datasets (read-only, when configured)",
                    "sessions": {
                        "create": "POST /sessions {command: [...], files: {...}} or {command: [...], version_id: ...}",
                        "list": "GET /sessions (managed descendants and parent relationships)",
                        "status": "GET /sessions/{session_id}",
                        "self_status": "GET /sessions/self",
                        "self_stop": "POST /sessions/self/stop {reason: ...}; save checkpoint and evidence first; descendants remain running",
                        "cancel": "DELETE /sessions/{session_id}",
                        "cancel_branch": "DELETE /sessions/{session_id}?cascade=true",
                        "artifact": "GET /sessions/{session_id}/artifacts?path=relative_path",
                        "workspace": "/workspace",
                        "public_a3": "/opt/a3 (read-only source; simulation runs in remote jobs)",
                        "identity": "host-injected independent identity; no parent token or workspace",
                        "max_active": MAX_ACTIVE_CANDIDATES,
                        "lifecycle": "Process exit preserves descendants; explicit branch cancel or run shutdown reclaims them",
                    },
                    "versions": {
                        "save": "POST /versions {files: {...}, parent_version: optional version_id}",
                        "list": "GET /versions (only explicitly authorized versions)",
                        "storage": "Host commits and worktrees; Docker receives only its source directory",
                    },
                    "comparison": "POST /checks/comparison {baseline: evidence, candidate: evidence}",
                    "diagnostics": "Host gateway-events.jsonl; response header X-Request-ID",
                    "isolation_fixture": {
                        "create": "POST /fixtures/isolation",
                        "verify": "GET /fixtures/isolation/{fixture_id}",
                        "close": "DELETE /fixtures/isolation/{fixture_id}",
                        "scope": "peer identity and sentinel remain host-owned; only peer job ID is shared",
                    },
                    "remote_limits": remote_limits(),
                    "execution_guide": "/run/input/execution.md",
                    "input_manifest": "/run/input/input-manifest.json",
                    "parent_run": start.previous_run.name
                    if start and start.previous_run
                    else parent.name
                    if parent
                    else None,
                },
                indent=2,
            )
        )
    else:
        deployment = json.loads((inputs / "deployment.json").read_text())
        deployment["sessions"]["max_active"] = MAX_ACTIVE_CANDIDATES
        deployment["remote_limits"] = remote_limits()
        (inputs / "deployment.json").write_text(json.dumps(deployment, indent=2))
    config = yaml.safe_load((inputs / "bootstrap.yaml").read_text())
    if args.verify_deployment:
        config["agent"].update(
            system_template=(
                "You are checking deployment readiness only. Follow the task without user input. "
                "Do not run the full research task. Save evidence, then submit using a bash command "
                "whose first output line is COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT."
            ),
            step_limit=24,
            wall_time_limit_seconds=600,
        )
        config["run"]["task"] = (inputs / "readiness-task.md").read_text()
    env = None
    normal_finish = False
    os.environ.update(MSWEA_GLOBAL_CALL_LIMIT="0", MSWEA_GLOBAL_COST_LIMIT="0")
    try:
        if package:
            command, files = package
            if private_inputs:
                for path in inputs.iterdir():
                    if path.is_file():
                        shutil.copy2(path, private_inputs / path.name)
            session = app.state.start_local_harness(
                command, files, "Bearer " + token, read_only_exports=initial_exports
            )
            (run / "startup.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "phase": input_versions.phase,
                        "mode": kind,
                        "package": str(args.run_harness or args.resume_from),
                        "parent_run": start.previous_run.name if start.previous_run else None,
                        "source_version": start.source_version,
                        "task_context": task_context,
                        "remote_exports": initial_exports,
                        "session_id": session["session_id"],
                        "version_id": session["version_id"],
                        "inputs": input_hashes(inputs),
                        "input_manifest": input_versions.manifest,
                    },
                    indent=2,
                )
            )
            (run.parent / "latest-run.txt").write_text(str(run) + "\n")
            print(f"Run directory: {run}", flush=True)
        if not resuming or resume_context is not None:
            env = DockerEnvironment(
                image=image,
                cwd="/workspace/a3",
                container_timeout="infinity",
                timeout=300,
                run_args=[
                    "--rm",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--pids-limit=256",
                    "--memory=4g",
                    "--cpus=2",
                    "--mount",
                    f"type=bind,src={workspace},dst=/workspace",
                    "--mount",
                    f"type=bind,src={private_inputs or inputs},dst=/run/input,readonly",
                    *(
                        ["--mount", f"type=bind,src={run / 'recovery'},dst=/run/recovery,readonly"]
                        if (run / "recovery").is_dir()
                        else []
                    ),
                ],
                env={
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "safe.directory",
                    "GIT_CONFIG_VALUE_0": "*",
                    "A3_GATEWAY_URL": endpoint,
                    "A3_GATEWAY_TOKEN": token,
                    "MSWEA_COST_TRACKING": "ignore_errors",
                    "OPENAI_API_BASE": endpoint + "/v1",
                    "OPENAI_API_KEY": token,
                    "A3_PROJECT_ROOT": "/workspace/a3",
                    "A3_HARNESS_ROOT": "/workspace/harness",
                    "A3_RUN_DIR": "/workspace/run",
                    "A3_GOAL_FILE": "/run/input/goal.md",
                    "A3_ACCEPTANCE_FILE": "/run/input/acceptance.md",
                    "A3_DEPLOYMENT_FILE": "/run/input/deployment.json",
                },
            )
        if not package and not resuming:
            (run / "startup.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "phase": input_versions.phase,
                        "inputs": input_hashes(inputs),
                        "input_manifest": input_versions.manifest,
                        "a3_commit": git(a3, "rev-parse", "HEAD"),
                        "harness_commit": git(ROOT, "rev-parse", "HEAD"),
                        "mini_commit": git(
                            ROOT / "third_party/mini-swe-agent", "rev-parse", "HEAD"
                        ),
                        "container_id": env.container_id,
                        "model": os.environ["MODEL_NAME"],
                        "parent_run": parent.name if parent else None,
                        "mode": kind,
                    },
                    indent=2,
                )
            )
        (run.parent / "latest-run.txt").write_text(str(run) + "\n")
        print(f"Run directory: {run}", flush=True)
        template_vars = {
            key: value
            for key, value in (env.config.env.items() if env is not None else [])
            if key.startswith("A3_") and key not in ("A3_GATEWAY_TOKEN", "A3_GATEWAY_URL")
        }

        template_vars["phase_instructions"] = phase_instructions

        def execute_builder(reason: str, trajectory: Path) -> None:
            model = get_model(
                config={
                    "model_name": os.environ["MODEL_NAME"],
                    "model_class": os.environ["MODEL_CLASS"],
                    "cost_tracking": os.environ.get("MODEL_COST_TRACKING", "default"),
                    **config.get("model", {}),
                }
            )
            agent_args = {**config["agent"], "output_path": trajectory}
            agent_args.pop("agent_class", None)
            agent = ContextAgent(model, env, **agent_args)
            if not args.verify_deployment:

                def current_system():
                    with input_versions.lock:
                        latest = yaml.safe_load(input_versions.contents["bootstrap.yaml"])
                        agent.extra_template_vars["phase_instructions"] = (
                            input_versions.model_instructions()[1]
                        )
                        return agent._render_template(latest["agent"]["system_template"])

                agent.system_instruction_provider = current_system
            if resume_context is not None:
                agent.extra_template_vars = {"task": config["run"]["task"], **template_vars}
                agent.resume(resume_context)
            else:
                agent.run(config["run"]["task"], **template_vars)

        def result_workspace() -> Path:
            actor, _ = app.state.result_location()
            if actor and actor != run_id:
                return (run / "candidates" / actor / "workspace").resolve()
            return workspace / "run"

        def save_completed_result() -> None:
            actor, parent_version = app.state.result_location()

            def validate_exports(identifiers):
                result = remote_for_run(
                    "exports-check",
                    {
                        "experiment_id": actor or run_id,
                        "export_ids": identifiers,
                    },
                )
                if result.get("status") == "error":
                    raise ValueError("Retained exports must be readable and sealed")

            selected = publish_run_result(
                run,
                result_workspace(),
                versions,
                actor or run_id,
                parent_version,
                validate_exports=validate_exports,
            )
            if selected["status"] != "ready":
                raise ValueError("Completed run needs a runnable retained snapshot")

        if resuming:
            sandbox.resume_sessions(saved["gateway"]["sessions"], actor)
            with (run / "resume-events.jsonl").open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "event": "resumed",
                            "time": time.time(),
                            "run_id": run_id,
                            "main_owner": actor,
                            "max_active": MAX_ACTIVE_CANDIDATES,
                            "input_manifest": input_versions.manifest,
                        }
                    )
                    + "\n"
                )
        store.update(status="running")
        if args.verify_deployment:
            execute_builder(config["run"]["task"], run / "trajectory.json")
        else:
            headers = {"Authorization": "Bearer " + token}
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=90
            ) as client:
                outcome = RunSupervisor(
                    run=run,
                    workspace=workspace / "run",
                    required_checks=input_versions.acceptance_checks,
                    sessions=lambda: supervised_sessions(client),
                    execute_builder=execute_builder,
                    finalize=save_completed_result,
                    result_workspace=result_workspace,
                    input_manifest=lambda: input_versions.manifest,
                    on_finishing=app.state.begin_shutdown,
                ).run(initial_builder=not package and (not resuming or resume_context is not None))
        if args.verify_deployment:
            publish_run_result(run, workspace / "run", versions, run_id, None)
        normal_finish = args.verify_deployment or outcome["status"] == "complete"
    except KeyboardInterrupt:
        print("Run interrupted; preserving identities and resources for --resume-from", flush=True)
    finally:
        app.state.begin_shutdown()
        cleanup_errors = []
        try:
            if not normal_finish:
                try:
                    sandbox.suspend()
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    cleanup_errors.append("candidate suspension")
                app.state.save_checkpoint()
                store.update(status="suspended")
            else:
                for label, cleanup in (
                    ("gateway resources", app.state.close_resources),
                    ("candidate containers", sandbox.close),
                    ("peer fixtures", fixtures.close),
                ):
                    try:
                        cleanup()
                    except Exception:  # noqa: BLE001 — preserve cleanup evidence and continue
                        cleanup_errors.append(label)
                store.update(status="completed")
            if cleanup_errors:
                (run / "cleanup-errors.json").write_text(json.dumps(cleanup_errors))
                print("Cleanup requires review: " + ", ".join(cleanup_errors), flush=True)
        finally:
            try:
                if env is not None and env.container_id:
                    subprocess.run(
                        ["docker", "rm", "-f", env.container_id], capture_output=True, check=False
                    )
                    env.container_id = None
            finally:
                server.should_exit = True
                thread.join(timeout=10)


if __name__ == "__main__":
    main()
