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
from minisweagent.agents import get_agent
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.models import get_model, get_model_class

from runtime.local.candidate_runtime import CandidateRuntime
from runtime.local.candidate_versions import CandidateVersions
from runtime.local.continuation import (
    publish_run_result,
    resolve_harness_start,
    resolve_resume_start,
)
from runtime.local.host_gateway import create_app
from runtime.local.isolation_fixture import IsolationFixtures
from runtime.local.recovery import stage_recovery

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
    shutil.copy2(ROOT / "runtime/local/README.md", inputs / "execution.md")


def validate_remote_health(response: dict) -> None:
    if response.get("status") != "ok" or response.get("protocol_version") != 1:
        raise RuntimeError("Remote controller health or protocol check failed")


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
    if args.resume_from:
        start = resolve_resume_start(args.resume_from.resolve())
    package = (start.command, start.files) if start else None
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
    kind = (
        "deployment-checks" if args.verify_deployment else "retained-harness" if package else "rsi"
    )
    run_root = ROOT / "artifacts"
    run_root /= "validation/deployment" if args.verify_deployment else "rsi"
    run = create_run_directory(run_root)
    run_id = run.name
    inputs, workspace = run / "inputs", run / "workspace"
    inputs.mkdir(parents=True, mode=0o700)
    workspace.mkdir()
    parent = args.resume_from.resolve() if args.resume_from else None
    if package:
        (workspace / "run").mkdir()
    else:
        prepare_workspace(workspace, a3, bootstrap_source(parent))
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
    if parent and start is None:
        stage_recovery(parent, run / "recovery")
        (inputs / "parent-run.json").write_text(
            json.dumps(
                {
                    "run_id": parent.name,
                    "recovery_index": "/run/recovery/index.json",
                    "instruction": "Inspect saved code, reflections and real results before choosing what to resume. Old sessions and credentials are not reusable.",
                },
                indent=2,
            )
        )
    shutil.copy2(ROOT / "configs/rsi-bootstrap.yaml", inputs / "bootstrap.yaml")
    public_a3 = run / "public/a3"
    public_a3.parent.mkdir()
    snapshot_checkout(a3, public_a3)
    token = secrets.token_urlsafe(32)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    endpoint = f"http://host.docker.internal:{port}"
    versions = CandidateVersions(run / "versions")
    sandbox = CandidateRuntime(
        root=run / "candidates",
        image=image,
        inputs=inputs,
        public_a3=public_a3,
        endpoint=endpoint,
        versions=versions,
    )
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
    )
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
                    "max_active": 2,
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
                "remote_limits": {"cpus": 2, "memory": "8g", "max_jobs": 2, "timeout_seconds": 900},
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
    config = yaml.safe_load((ROOT / "configs/rsi-bootstrap.yaml").read_text())
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
    (inputs / "bootstrap.yaml").write_text(yaml.safe_dump(config, allow_unicode=True))
    env = None
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
                            "inputs": {
                                name: hashlib.sha256((inputs / name).read_bytes()).hexdigest()
                                for name in ("goal.md", "acceptance.md")
                            },
                        },
                        indent=2,
                    )
                )
                (run.parent / "latest-run.txt").write_text(str(run) + "\n")
                print(f"Run directory: {run}", flush=True)
                while True:
                    response = client.get("/sessions/" + session["session_id"])
                    response.raise_for_status()
                    status = response.json()
                    (run / "retained-status.json").write_text(json.dumps(status))
                    children = client.get("/sessions")
                    children.raise_for_status()
                    active = []
                    for child in children.json()["sessions"]:
                        if child["closed"]:
                            continue
                        current = client.get("/sessions/" + child["session_id"])
                        current.raise_for_status()
                        if current.json()["status"] in {
                            "running",
                            "created",
                            "restarting",
                            "paused",
                        }:
                            active.append(child["session_id"])
                    if not active:
                        print(f"Retained session exited: {status.get('exit_code')}", flush=True)
                        if status.get("exit_code") not in (0, None):
                            raise RuntimeError("Retained Harness exited unsuccessfully")
                        selected = publish_run_result(
                            run,
                            (run / "candidates" / session["session_id"] / "workspace").resolve(),
                            versions,
                            session["session_id"],
                            session["version_id"],
                        )
                        print(f"Next-run retained result: {selected['status']}", flush=True)
                        break
                    time.sleep(2)
            return
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
                f"type=bind,src={inputs},dst=/run/input,readonly",
                *(
                    ["--mount", f"type=bind,src={run / 'recovery'},dst=/run/recovery,readonly"]
                    if parent
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
        model = get_model(
            config={
                "model_name": os.environ["MODEL_NAME"],
                "model_class": os.environ["MODEL_CLASS"],
                "cost_tracking": os.environ.get("MODEL_COST_TRACKING", "default"),
                **config.get("model", {}),
            }
        )
        agent_args = config["agent"].copy()
        agent_args["output_path"] = run / "trajectory.json"
        agent = get_agent(model, env, agent_args)
        (run / "startup.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "inputs": {
                        name: hashlib.sha256((inputs / name).read_bytes()).hexdigest()
                        for name in ("goal.md", "acceptance.md", "bootstrap.yaml")
                    },
                    "a3_commit": git(a3, "rev-parse", "HEAD"),
                    "harness_commit": git(ROOT, "rev-parse", "HEAD"),
                    "mini_commit": git(ROOT / "third_party/mini-swe-agent", "rev-parse", "HEAD"),
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
            for key, value in env.config.env.items()
            if key.startswith("A3_") and key not in ("A3_GATEWAY_TOKEN", "A3_GATEWAY_URL")
        }
        agent.run(config["run"]["task"], **template_vars)
        publish_run_result(run, workspace / "run", versions, run_id, None)
    finally:
        app.state.begin_shutdown()
        cleanup_errors = []
        try:
            for label, cleanup in (
                ("gateway resources", app.state.close_resources),
                ("candidate containers", sandbox.close),
                ("peer fixtures", fixtures.close),
            ):
                try:
                    cleanup()
                except Exception:  # noqa: BLE001 — preserve cleanup evidence and continue
                    cleanup_errors.append(label)
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
