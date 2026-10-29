"""Launch immutable upstream mini against an experiment-only Docker workspace."""

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
import uuid
from pathlib import Path

import litellm
import uvicorn
import yaml
from dotenv import load_dotenv
from minisweagent.agents import get_agent
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.models import get_model, get_model_class

from runtime.local.host_gateway import create_app

ROOT = Path(__file__).resolve().parents[2]


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def snapshot_checkout(source: Path, destination: Path) -> None:
    subprocess.run(
        ["git", "clone", "--quiet", "--no-local", str(source), str(destination)], check=True
    )
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


def prepare_workspace(workspace: Path, a3: Path) -> None:
    snapshot_checkout(a3, workspace / "a3")
    snapshot_checkout(ROOT, workspace / "harness")
    (workspace / "run").mkdir()
    (workspace / "harness/AGENTS.md").write_text(
        "本次在独立容器中冷启动。模型和作业均经宿主 gateway 转发。\n"
        "不得尝试获得宿主凭据或改变资源分配。专项研究逻辑由你实现。\n"
        "读取 /run/input/goal.md 与 /run/input/acceptance.md。\n"
    )


def validate_remote_health(response: dict) -> None:
    if response.get("status") != "ok" or response.get("protocol_version") != 1:
        raise RuntimeError("Remote controller health or protocol check failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
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
    run_id = "run-" + uuid.uuid4().hex
    run = ROOT / "artifacts/rsi" / run_id
    inputs, workspace = run / "inputs", run / "workspace"
    inputs.mkdir(parents=True, mode=0o700)
    workspace.mkdir()
    prepare_workspace(workspace, a3)
    for name in ("goal.md", "acceptance.md"):
        shutil.copy2(ROOT / "evals" / name, inputs / name)
    shutil.copy2(ROOT / "configs/rsi-bootstrap.yaml", inputs / "bootstrap.yaml")
    token = secrets.token_urlsafe(32)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = create_app(
        model_name=os.environ["MODEL_NAME"],
        sessions={token: run_id},
        forward_model=litellm.responses,
        remote_call=remote_call,
        usage_log=run / "model-usage.jsonl",
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
    endpoint = f"http://host.docker.internal:{port}"
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
            },
            indent=2,
        )
    )
    config = yaml.safe_load((ROOT / "configs/rsi-bootstrap.yaml").read_text())
    env = None
    os.environ.update(MSWEA_GLOBAL_CALL_LIMIT="0", MSWEA_GLOBAL_COST_LIMIT="0")
    try:
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
    finally:
        if env is not None and env.container_id:
            subprocess.run(
                ["docker", "rm", "-f", env.container_id], capture_output=True, check=False
            )
            env.container_id = None
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    main()
