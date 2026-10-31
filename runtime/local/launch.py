"""Launch a cold start, resume a run, or continue a retained Harness."""

import argparse
import hashlib
import json
import os
import re
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
    publish_run_result,
    resolve_harness_start,
)
from runtime.local.host_gateway import create_app
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
    command = shlex.join(
        [
            os.environ.get("A3_REMOTE_PYTHON", "python3"),
            os.environ["A3_REMOTE_CONTROLLER"],
            "--root",
            os.environ["A3_REMOTE_RUN_ROOT"],
            "--image",
            os.environ["A3_REMOTE_IMAGE"],
            operation,
        ]
    )
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
        "本次在独立容器中冷启动。模型和作业均经宿主 gateway 转发。\n"
        "不得尝试获得宿主凭据或改变资源分配。专项研究逻辑由你实现。\n"
        "读取 /run/input/goal.md 与 /run/input/acceptance.md。\n"
        "宿主提供候选执行与双身份验收入口，先读 /run/input/execution.md。\n"
    )


def bootstrap_source(parent: Path | None) -> Path:
    if parent and (parent / "workspace/harness/.git").exists():
        return parent / "workspace/harness"
    return ROOT


def freeze_inputs(inputs: Path, parent: Path | None = None) -> None:
    source = parent / "inputs" if parent else ROOT / "evals"
    hashes = json.loads((parent / "startup.json").read_text())["inputs"] if parent else {}
    for name in ("goal.md", "acceptance.md"):
        content = (source / name).read_bytes()
        if parent and hashlib.sha256(content).hexdigest() != hashes.get(name):
            raise ValueError(f"Frozen input hash mismatch: {name}")
    for name in ("goal.md", "acceptance.md"):
        shutil.copy2(source / name, inputs / name)
    if parent:
        (inputs / "parent-run.json").write_text(json.dumps({"run_id": parent.name}, indent=2))
        blocker = parent / "workspace/run/blocker.md"
        if blocker.is_file():
            shutil.copy2(blocker, inputs / "previous-blocker.md")
    if parent:
        for name in ("execution.md", "bootstrap.yaml"):
            content = (source / name).read_bytes()
            if name in hashes and hashlib.sha256(content).hexdigest() != hashes[name]:
                raise ValueError(f"Frozen input hash mismatch: {name}")
            shutil.copy2(source / name, inputs / name)
    else:
        shutil.copy2(ROOT / "runtime/local/README.md", inputs / "execution.md")
    common = source / "common-instructions.md" if parent else ROOT / "configs/prompts/common.md"
    if common.is_file():
        content = common.read_bytes()
        if parent and hashes.get("common-instructions.md") != hashlib.sha256(content).hexdigest():
            raise ValueError("Frozen input hash mismatch: common-instructions.md")
        shutil.copy2(common, inputs / "common-instructions.md")


def frozen_input_hashes(inputs: Path) -> dict[str, str]:
    names = ("goal.md", "acceptance.md", "bootstrap.yaml", "execution.md", "common-instructions.md")
    return {
        name: hashlib.sha256((inputs / name).read_bytes()).hexdigest()
        for name in names
        if (inputs / name).is_file()
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
        "--resume-from", type=Path, help="Continue a saved run with its frozen goals"
    )
    parser.add_argument(
        "--run-harness",
        type=Path,
        help="Start from the retained result of an explicitly selected run/package directory",
    )
    args = parser.parse_args()
    if args.run_harness and (args.resume_from or args.verify_deployment):
        parser.error("--run-harness cannot be combined with --resume-from or --verify-deployment")
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
    if resuming:
        startup = json.loads((run / "startup.json").read_text())
        for name, checksum in startup["inputs"].items():
            if hashlib.sha256((run / "inputs" / name).read_bytes()).hexdigest() != checksum:
                raise ValueError(f"Frozen input hash mismatch: {name}")
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
        freeze_inputs(inputs, parent)
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
        if not parent:
            shutil.copy2(ROOT / "configs/rsi-bootstrap.yaml", inputs / "bootstrap.yaml")
        public_a3 = run / "public/a3"
        public_a3.parent.mkdir()
        snapshot_checkout(a3, public_a3)
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
    common_path = inputs / "common-instructions.md"
    common_instructions = common_path.read_text() if common_path.is_file() else ""
    if args.verify_deployment:
        common_instructions = ""  # Readiness checks are test sessions, not main Agents.
    fixtures = IsolationFixtures(remote_call, run / "fixtures")
    app = create_app(
        model_name=os.environ["MODEL_NAME"],
        sessions={token: run_id},
        forward_model=litellm.responses,
        remote_call=remote_call,
        usage_log=run / "model-usage.jsonl",
        sandbox=sandbox,
        fixtures=fixtures,
        versions=versions,
        initial_inputs=private_inputs,
        main_owner=saved["gateway"]["evolution"]["main_owner"] if saved else run_id,
        start_main_session=bool(package),
        common_instructions=common_instructions,
        state_store=store,
        restored_state=saved["gateway"] if saved else None,
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
                    "sessions": {
                        "create": "POST /sessions {command: [...], files: {...}} or {command: [...], version_id: ...}",
                        "list": "GET /sessions (managed descendants and parent relationships)",
                        "status": "GET /sessions/{session_id}",
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
                    "remote_limits": {
                        "cpus": 2,
                        "memory": "8g",
                        "max_jobs": 2,
                        "timeout_seconds": 900,
                    },
                    "execution_guide": "/run/input/execution.md",
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
        config["run"]["task"] = (ROOT / "runtime/local/readiness-task.md").read_text()
    if not resuming:
        (inputs / "bootstrap.yaml").write_text(yaml.safe_dump(config, allow_unicode=True))
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
            headers = {"Authorization": "Bearer " + token}

            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=90
            ) as client:
                response = client.post("/sessions", json={"command": command, "files": files})
                response.raise_for_status()
                session = response.json()
                (run / "startup.json").write_text(
                    json.dumps(
                        {
                            "run_id": run_id,
                            "mode": kind,
                            "package": str(args.run_harness or args.resume_from),
                            "parent_run": start.previous_run.name if start.previous_run else None,
                            "source_version": start.source_version,
                            "session_id": session["session_id"],
                            "version_id": session["version_id"],
                            "inputs": frozen_input_hashes(inputs),
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
                        "inputs": frozen_input_hashes(inputs),
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

        template_vars["common_instructions"] = common_instructions

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
            selected = publish_run_result(
                run, result_workspace(), versions, actor or run_id, parent_version
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
                        }
                    )
                    + "\n"
                )
        store.update(status="running")
        if args.verify_deployment:
            execute_builder(config["run"]["task"], run / "trajectory.json")
        else:
            required_checks = tuple(
                re.findall(r"^## ([A-Z])\.", (inputs / "acceptance.md").read_text(), re.MULTILINE)
            )
            headers = {"Authorization": "Bearer " + token}
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=90
            ) as client:
                outcome = RunSupervisor(
                    run=run,
                    workspace=workspace / "run",
                    required_checks=required_checks,
                    sessions=lambda: supervised_sessions(client),
                    execute_builder=execute_builder,
                    finalize=save_completed_result,
                    result_workspace=result_workspace,
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
