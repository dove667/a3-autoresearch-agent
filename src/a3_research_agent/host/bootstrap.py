"""Launch a cold start, resume a run, or continue a retained Harness."""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import litellm
import uvicorn
import yaml
from dotenv import load_dotenv
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.models import get_model, get_model_class

from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.inputs.feedback import MAX_FEEDBACK_BYTES, HumanFeedback
from a3_research_agent.host.inputs.versions import (
    EXTENSION_PROFILES,
    PHASES,
    TASK_PROFILES,
    InputVersions,
    resolve_phase,
    resolve_task_profile,
)
from a3_research_agent.host.models.context import read_context_budget
from a3_research_agent.host.models.settings import read_model_request_options, read_role_models
from a3_research_agent.host.models.upstream import configured_upstream
from a3_research_agent.host.runs.context import ContextAgent, load_context
from a3_research_agent.host.runs.continuation import (
    import_retained_exports,
    publish_run_result,
    resolve_harness_start,
)
from a3_research_agent.host.runs.recovery import stage_recovery
from a3_research_agent.host.runs.resume import (
    begin_blocked_resume,
    finish_blocked_resume,
    load_resume_state,
    preflight_blocked_resume,
)
from a3_research_agent.host.runs.state import RunLease, RunStateStore
from a3_research_agent.host.runs.supervisor import RunSupervisor
from a3_research_agent.host.sessions.containers import MAX_ACTIVE_CANDIDATES, CandidateRuntime
from a3_research_agent.host.sessions.isolation import IsolationFixtures
from a3_research_agent.host.sessions.versions import CandidateVersions
from a3_research_agent.host.web.gateway import WebConfig, WebGateway
from a3_research_agent.project import checkout_root, resolve_project_root

__all__ = [
    "EXTENSION_PROFILES",
    "MAX_FEEDBACK_BYTES",
    "PHASES",
    "ROOT",
    "TASK_PROFILES",
    "HumanFeedback",
    "InputVersions",
    "Path",
    "RunLease",
    "argparse",
    "create_run_directory",
    "get_model_class",
    "git",
    "input_hashes",
    "json",
    "launch_run",
    "load_dotenv",
    "local_storage_bytes",
    "os",
    "prepare_custom_task",
    "prepare_task_start",
    "remote_call",
    "remote_limits",
    "resolve_harness_start",
    "resolve_phase",
    "resolve_project_root",
    "resolve_task_profile",
    "snapshot_checkout",
    "subprocess",
    "validate_remote_health",
    "validate_skills_source",
]

ROOT = checkout_root()

from a3_research_agent.host.inputs.task_preparation import (
    input_hashes,
    prepare_custom_task,
    prepare_task_start,
    validate_skills_source,
)
from a3_research_agent.host.runs.directories import create_run_directory, local_storage_bytes
from a3_research_agent.host.transport import (
    remote_call,
    remote_limits,
    remote_path_hints,
    validate_remote_health,
)
from a3_research_agent.host.workspaces import git, snapshot_checkout


def prepare_workspace(workspace: Path, a3: Path, harness: Path | None = None) -> None:
    snapshot_checkout(a3, workspace / "a3")
    snapshot_checkout(harness or ROOT, workspace / "harness")
    (workspace / "run").mkdir()
    (workspace / "harness/AGENTS.md").write_text(
        (ROOT / "agent-inputs/shared/workspace.md").read_text()
    )


def bootstrap_source(parent: Path | None) -> Path:
    if parent and (parent / "workspace/harness/.git").exists():
        return parent / "workspace/harness"
    return ROOT


def supervised_sessions(client: httpx.Client) -> list[dict]:
    response = client.get("/sessions", params={"include_status": "true"})
    response.raise_for_status()
    return response.json()["sessions"]


def launch_run(args, run: Path, a3: Path, image: str, start) -> None:
    model_request_options = read_model_request_options(os.environ)
    role_models = read_role_models(os.environ)
    context_budget = read_context_budget(os.environ)
    package = (start.command, start.files) if start else None
    kind = (
        "deployment-checks" if args.verify_deployment else "retained-harness" if package else "rsi"
    )
    resuming = args.resume_from is not None
    store = RunStateStore(run)
    saved = None
    resume_context = None
    blocked_plan = None
    blocked_attempt = None
    feedback_content = None
    registration = None
    proposed_inputs = getattr(args, "custom_input_proposal", None)
    if proposed_inputs:
        input_versions = InputVersions.from_proposal(ROOT, run, proposed_inputs)
    else:
        input_versions = InputVersions(
            ROOT,
            run,
            phase=getattr(args, "phase", None),
            task_profile=getattr(args, "task_profile", None),
        )
        proposed_inputs = input_versions.prepare()
    if resuming:
        startup = json.loads((run / "startup.json").read_text())
        saved = load_resume_state(run, store, os.environ["MODEL_NAME"], os.environ["MODEL_CLASS"])
        actor = saved["gateway"]["evolution"]["main_owner"]
        feedback_proposal = None
        if getattr(args, "feedback_file", None):
            with args.feedback_file.open("rb") as stream:
                feedback_content = stream.read(MAX_FEEDBACK_BYTES + 1)
            text = HumanFeedback.validate_content(feedback_content)
            feedback_proposal = HumanFeedback.preview(text, actor)
        blocked_plan = preflight_blocked_resume(run, saved, feedback=feedback_proposal)
        if actor == run.name:
            checkpoint = json.loads((run / "trajectory.json").read_text())
            if checkpoint.get("info", {}).get("exit_status") != "Submitted":
                resume_context = load_context(run / "trajectory.json")
    forward_model = configured_upstream(litellm.responses)
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
    if package:
        start, task_context = prepare_task_start(
            start,
            input_versions.phase,
            input_versions.task_profile,
            input_versions.contents["goal.md"],
            initial_feedback=getattr(args, "initial_feedback", None),
            initial_feedback_source=getattr(args, "initial_feedback_file", None),
            custom_inputs=input_versions.input_mode == "custom",
        )
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
        if operation == "submit" or (operation == "jobs-list" and "run_id" not in payload):
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
        registration = human_feedback.register_content(feedback_content, actor)
        print("Human feedback registered: " + json.dumps(registration), flush=True)
    web_gateway = WebGateway(
        run / ".host" / "web", WebConfig.from_env(), cache_root=ROOT / "artifacts" / "web-cache"
    )
    if blocked_plan is not None:
        blocked_attempt = begin_blocked_resume(
            run, saved, store, sandbox, blocked_plan, input_versions.manifest, registration
        )
    app = create_app(
        web_gateway=web_gateway,
        model_name=os.environ["MODEL_NAME"],
        sessions={token: run_id},
        forward_model=forward_model,
        model_request_options=model_request_options,
        role_models=role_models,
        context_budget=context_budget,
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
        project_history_run=run
        if input_versions.phase == "phase-two" and not args.verify_deployment
        else None,
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
    previous_deployment = (inputs / "deployment.json").read_bytes() if resuming else None
    if not resuming:
        (inputs / "deployment.json").write_text(
            json.dumps(
                {
                    "version": "0.2.3",
                    "gateway": endpoint,
                    "web": web_gateway.status(),
                    "workspace": "/workspace",
                    "jobs": "POST /jobs {command: [...], gpu: bool, files: {relative_path: text}}",
                    "status": "GET /jobs/{job_id}",
                    "wait": "POST /jobs/wait {job_ids:[...],seconds:25}; renew timeouts inside Harness without a model call",
                    "cancel": "DELETE /jobs/{job_id}",
                    "artifact": "GET /jobs/{job_id}/artifacts?path=relative_path",
                    "authorization": "Bearer $A3_GATEWAY_TOKEN",
                    "model": "openai/sandbox-model",
                    "model_class": "litellm_response",
                    "model_request_options": model_request_options,
                    "model_roles": role_models,
                    "context_budget": context_budget,
                    "cost_tracking": "ignore_errors",
                    "usage_accounting": "host gateway model-usage.jsonl; alias prices are unknown",
                    "model_api_base": endpoint + "/v1",
                    "model_api_key": "$A3_GATEWAY_TOKEN",
                    **remote_path_hints(),
                    "sessions": {
                        "create": "No worker creation; POST /sessions is disabled for Agent credentials",
                        "trial": "Cold-start bootstrap only: POST /evolution/trials {baseline_version, checklist, command: [...], files: {...}} for the first research session",
                        "list": "GET /sessions (all registered research identities in this run)",
                        "read_scope": "Same-run research source, memory, logs, versions, job state, artifacts and authorized readonly exports are shared; mutations retain owner/adopted-lineage checks",
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
        deployment["version"] = "0.2.3"
        deployment["wait"] = (
            "POST /jobs/wait {job_ids:[...],seconds:25}; renew timeouts inside Harness without a model call"
        )
        deployment["sessions"]["create"] = (
            "No worker creation; POST /sessions is disabled for Agent credentials"
        )
        deployment["sessions"]["list"] = (
            "GET /sessions (all registered research identities in this run)"
        )
        deployment["sessions"]["read_scope"] = (
            "Same-run research source, memory, logs, versions, job state, artifacts and authorized readonly exports are shared; mutations retain owner/adopted-lineage checks"
        )
        deployment["sessions"]["max_active"] = MAX_ACTIVE_CANDIDATES
        deployment["remote_limits"] = remote_limits()
        deployment.update(remote_path_hints())
        deployment["web"] = web_gateway.status()
        (inputs / "deployment.json").write_text(json.dumps(deployment, indent=2))
    current_deployment = (inputs / "deployment.json").read_bytes()
    if previous_deployment is not None and previous_deployment != current_deployment:
        previous_hash = hashlib.sha256(previous_deployment).hexdigest()
        archive = run / ".host/deployment-revisions" / (previous_hash + ".json")
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists():
            archive.write_bytes(previous_deployment)
        with (run / ".host/deployment-events.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "version": "0.2.3",
                        "previous_sha256": previous_hash,
                        "sha256": hashlib.sha256(current_deployment).hexdigest(),
                        "time": time.time(),
                    }
                )
                + "\n"
            )
    if private_inputs:
        (private_inputs / "deployment.json").write_bytes(current_deployment)
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
                        "task_profile": input_versions.task_profile,
                        "mode": kind,
                        "model_request_options": model_request_options,
                        "model_roles": role_models,
                        "context_budget": context_budget,
                        "model": os.environ["MODEL_NAME"],
                        "model_class": os.environ["MODEL_CLASS"],
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
                        "task_profile": input_versions.task_profile,
                        "inputs": input_hashes(inputs),
                        "input_manifest": input_versions.manifest,
                        "a3_commit": git(a3, "rev-parse", "HEAD"),
                        "harness_commit": git(ROOT, "rev-parse", "HEAD"),
                        "mini_commit": git(
                            ROOT / "third_party/mini-swe-agent", "rev-parse", "HEAD"
                        ),
                        "container_id": env.container_id,
                        "model": os.environ["MODEL_NAME"],
                        "model_class": os.environ["MODEL_CLASS"],
                        "parent_run": parent.name if parent else None,
                        "mode": kind,
                        "model_request_options": model_request_options,
                        "model_roles": role_models,
                        "context_budget": context_budget,
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
            model_config = {
                **config.get("model", {}),
                "model_name": os.environ["MODEL_NAME"],
                "model_class": os.environ["MODEL_CLASS"],
                "cost_tracking": os.environ.get("MODEL_COST_TRACKING", "default"),
            }
            model_config["model_kwargs"] = {
                **model_config.get("model_kwargs", {}),
                **model_request_options,
            }
            model = get_model(config=model_config)
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
                app.state.validate_exports(actor or run_id, identifiers)

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
            if blocked_attempt is not None:
                finish_blocked_resume(blocked_attempt)
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


def main() -> None:
    import sys

    from a3_research_agent.cli.launch import launch_main

    launch_main(sys.modules[__name__])


if __name__ == "__main__":
    main()
