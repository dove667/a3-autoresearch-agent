"""Start and supervise a published native Harness in its persistent research workspace."""

import contextlib
import hashlib
import json
import os
import secrets
import signal
import socket
import threading
import time
from pathlib import Path

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import httpx
import litellm
import uvicorn

from a3_research_agent.host.dashboard.lifecycle import DashboardService
from a3_research_agent.host.gateway.app import create_app
from a3_research_agent.host.inputs.feedback import MAX_FEEDBACK_BYTES, HumanFeedback
from a3_research_agent.host.inputs.research_preparation import input_hashes, prepare_research_start
from a3_research_agent.host.inputs.versions import InputVersions
from a3_research_agent.host.knowledge_service.lifecycle import KnowledgeService
from a3_research_agent.host.models.settings import model_settings, require_release_model
from a3_research_agent.host.models.upstream import configured_upstream
from a3_research_agent.host.published_harness import (
    native_source,
    pin_run,
    project_for_run,
    run_commit,
    run_identity,
)
from a3_research_agent.host.reference_sources import link_reference_source
from a3_research_agent.host.remote_client import remote_call, remote_limits, remote_path_hints
from a3_research_agent.host.runs.continuation import import_retained_exports, publish_run_result
from a3_research_agent.host.runs.directories import debug_directory, local_storage_bytes
from a3_research_agent.host.runs.resume import (
    begin_blocked_resume,
    finish_blocked_resume,
    load_resume_state,
    preflight_blocked_resume,
)
from a3_research_agent.host.runs.state import RunStateStore
from a3_research_agent.host.runs.supervisor import SIGINT_EXIT_CODE, RunSupervisor
from a3_research_agent.host.runs.suspension import suspend_interrupted
from a3_research_agent.host.sessions.isolation import IsolationFixtures
from a3_research_agent.host.sessions.processes import ProcessRuntime
from a3_research_agent.host.sessions.workspace import MAX_ACTIVE_SESSIONS, write_durable
from a3_research_agent.host.startup_checks import record as record_startup_checks
from a3_research_agent.host.startup_checks import submit as submit_startup_checks
from a3_research_agent.host.web.gateway import WebConfig, WebGateway


def supervised_sessions(client: httpx.Client) -> list[dict]:
    response = client.get("/sessions", params={"include_status": "true"})
    response.raise_for_status()
    return response.json()["sessions"]


def launch_run(args, run: Path, a3: Path, start) -> None:
    """Own local semantic retrieval for the complete research process lifetime."""
    with contextlib.ExitStack() as services:
        _launch_run(args, run, a3, start, services)


def _launch_run(args, run: Path, a3: Path, start, services) -> None:
    """Launch a fixed published source or resume the same saved research process."""
    if args.resume_from and (run / ".host/pending-release.json").exists():
        raise ValueError("A release is pending; finish offline apply-release before resume")
    root = project_for_run(run)
    commit = run_commit(run) if args.resume_from else start.harness_commit if start else None
    if commit is None:
        raise ValueError("Host launch requires published Harness source")
    pinned_source = native_source(root, commit)
    settings = model_settings(os.environ)
    model_request_options, role_models, context_budget = (
        settings["model_request_options"],
        settings["model_roles"],
        settings["context_budget"],
    )
    resuming = args.resume_from is not None
    package = (start.command, start.files) if start else None
    run_id = run_identity(run)
    inputs, public_a3 = run / "inputs", run / "public/a3"
    store = RunStateStore(run)
    saved, session_runtime, blocked_plan, registration, feedback_content = (
        None,
        None,
        None,
        None,
        None,
    )
    dashboard = None
    proposed_inputs = getattr(args, "input_proposal", None)
    if proposed_inputs:
        input_versions = InputVersions.from_proposal(root, run, proposed_inputs)
    else:
        input_versions = InputVersions(root, run)
        proposed_inputs = input_versions.prepare()
    if resuming:
        saved = load_resume_state(run, store, settings["model"])
        require_release_model(saved, os.environ)
        actor = saved["gateway"]["main_owner"]
        feedback_proposal = None
        if getattr(args, "feedback_file", None):
            with args.feedback_file.open("rb") as stream:
                feedback_content = stream.read(MAX_FEEDBACK_BYTES + 1)
            feedback_proposal = HumanFeedback.preview(
                HumanFeedback.validate_content(feedback_content), actor
            )
        blocked_plan = preflight_blocked_resume(run, saved, feedback=feedback_proposal)
        port = int(saved["endpoint"].rsplit(":", 1)[1])
        token = next(key for key, value in saved["gateway"]["sessions"].items() if value == run_id)
        session_runtime = ProcessRuntime(
            root=run / "sessions",
            inputs=inputs,
            public_a3=public_a3,
            endpoint=f"http://127.0.0.1:{port}",
            published_source=pinned_source,
        )
        session_runtime.load_records()
        session_runtime.preflight_resume(actor)
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    else:
        token = secrets.token_urlsafe(32)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        inputs.mkdir(parents=True, mode=0o700)
        pin_run(run, commit)
        link_reference_source(a3, root / ".runtime/reference-sources", public_a3)
    endpoint = f"http://127.0.0.1:{port}"
    input_versions.activate(proposed_inputs, "resume" if resuming else "start")
    task_context = None
    if package:
        start, task_context = prepare_research_start(start)
        package = (start.command, start.files)
    forward_model = configured_upstream(litellm.responses)
    store.update(
        version=1,
        backend="process",
        harness_commit=commit,
        run_id=run_id,
        **settings,
        endpoint=endpoint,
        status="starting",
        operator_token=secrets.token_urlsafe(32),
    )
    if session_runtime is None:
        session_runtime = ProcessRuntime(
            root=run / "sessions",
            inputs=inputs,
            public_a3=public_a3,
            endpoint=endpoint,
            published_source=pinned_source,
        )
    external_knowledge_url = os.environ.get("A3_KNOWLEDGE_SERVICE_URL", "").strip()
    external_knowledge_token = os.environ.get("A3_KNOWLEDGE_SERVICE_TOKEN", "").strip()
    if external_knowledge_url and external_knowledge_token:
        # One service can serve several benchmark Hosts; each Host must not start
        # another embedding model and index over the same read-only knowledge.
        session_runtime.knowledge_environment = {
            "A3_KNOWLEDGE_SERVICE_URL": external_knowledge_url,
            "A3_KNOWLEDGE_SERVICE_TOKEN": external_knowledge_token,
            "A3_RUN_ID": run_id,
        }
    else:
        knowledge = services.enter_context(KnowledgeService(root, run, run_id))
        session_runtime.knowledge_environment = knowledge.environment
    task_instructions = input_versions.model_instructions()[1]

    def remote_for_run(operation, payload):
        if operation == "submit" or (operation == "jobs-list" and "run_id" not in payload):
            payload = {**payload, "run_id": run_id}
        return remote_call(operation, payload)

    def report_storage():
        return remote_for_run(
            "usage", {"run_id": run_id, "host_storage_bytes": local_storage_bytes(run)}
        )

    fixtures = IsolationFixtures(remote_for_run, run / "fixtures")
    initial_exports = import_retained_exports(start, run_id, remote_for_run)
    human_feedback = HumanFeedback(run)
    if feedback_content is not None:
        registration = human_feedback.register_content(feedback_content, actor)
    web_gateway = WebGateway(run / ".host/web", WebConfig.from_env(), cache_root=root / "web-cache")
    blocked_attempt = (
        begin_blocked_resume(
            run, saved, store, session_runtime, blocked_plan, input_versions.manifest, registration
        )
        if blocked_plan is not None
        else None
    )
    app = create_app(
        web_gateway=web_gateway,
        model_name=settings["model"],
        sessions={token: run_id},
        forward_model=forward_model,
        model_request_options=model_request_options,
        role_models=role_models,
        context_budget=context_budget,
        remote_call=remote_for_run,
        usage_log=run / "model-usage.jsonl",
        debug_root=debug_directory(run),
        session_runtime=session_runtime,
        fixtures=fixtures,
        initial_inputs=inputs,
        main_owner=saved["gateway"]["main_owner"] if saved else run_id,
        task_instructions=task_instructions,
        human_feedback=human_feedback,
        state_store=store,
        project_history_run=run,
        restored_state=saved["gateway"] if saved else None,
        input_versions=input_versions,
        operator_token=store.load()["operator_token"],
        resource_monitor=report_storage
        if int(os.environ.get("A3_REMOTE_RUN_STORAGE_BYTES", "0"))
        else None,
    )
    app.state.save_checkpoint()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
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
    deployment = {
        "version": "0.3.0",
        "harness_commit": commit,
        "local_execution": "process",
        "gateway": endpoint,
        "web": web_gateway.status(),
        "workspace": ".",
        "jobs": "POST /jobs {command: [...], gpu: bool, files: {relative_path: text}}",
        "status": "GET /jobs/{job_id}",
        "wait": "POST /jobs/wait {job_ids:[...],seconds:25}",
        "cancel": "DELETE /jobs/{job_id}",
        "artifact": "GET /jobs/{job_id}/artifacts?path=relative_path&offset=0&limit=4000",
        "authorization": "Bearer $A3_GATEWAY_TOKEN",
        "model": "openai/sandbox-model",
        "model_request_options": model_request_options,
        "model_roles": role_models,
        "context_budget": context_budget,
        "model_api_base": endpoint + "/v1",
        "model_api_key": "$A3_GATEWAY_TOKEN",
        "usage_accounting": "Host model-usage.jsonl",
        **remote_path_hints(),
        "sessions": {
            "list": "GET /sessions",
            "status": "GET /sessions/{session_id}",
            "self_status": "GET /sessions/self",
            "self_stop": "POST /sessions/self/stop {reason: ...}; save checkpoint first",
            "cancel": "DELETE /sessions/{session_id}",
            "artifact": "GET /sessions/{session_id}/artifacts?path=relative_path",
            "max_active": MAX_ACTIVE_SESSIONS,
        },
        "remote_limits": remote_limits(),
        "execution_guide": str(inputs / "execution.md"),
        "input_manifest": str(inputs / "input-manifest.json"),
    }
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
                        "version": "0.3.0",
                        "previous_sha256": previous_hash,
                        "sha256": hashlib.sha256(current_deployment).hexdigest(),
                        "time": time.time(),
                    }
                )
                + "\n"
            )
    interrupted = False
    exit_code = 0
    previous_sigint = None
    supervisor = None
    # `kill` sends SIGTERM; treat it as Ctrl-C so the checkpoint and the
    # supervision record are written either way.
    previous_sigterm = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        if package:
            command, files = package
            write_durable(
                run / "startup.json",
                json.dumps(
                    {
                        "run_id": run_id,
                        "version": 1,
                        "backend": "process",
                        "mode": "published-harness",
                        **settings,
                        "package": str(args.run_harness or getattr(args, "harness_commit", None)),
                        "parent_run": run_identity(start.previous_run)
                        if start.previous_run
                        else None,
                        "source_version": start.source_version,
                        "harness_commit": commit,
                        "task_context": task_context,
                        "remote_exports": initial_exports,
                        "inputs": input_hashes(inputs),
                        "input_manifest": input_versions.manifest,
                    },
                    indent=2,
                ),
            )
            session = app.state.start_local_harness(
                command, files, "Bearer " + token, read_only_exports=initial_exports
            )
            write_durable(
                run / ".host/startup-receipt.json",
                json.dumps({"session_id": session["session_id"], "time": time.time()}),
            )
        (run.parent / "latest-run.txt").write_text(str(run) + "\n")
        print(f"Run directory: {run}", flush=True)

        def result_workspace():
            actor = app.state.result_location()
            return (run / "sessions" / actor / "workspace").resolve()

        def retain_research():
            actor = app.state.result_location()
            publish_run_result(
                run,
                result_workspace(),
                actor,
                validate_exports=lambda identifiers: app.state.validate_exports(actor, identifiers),
            )

        if resuming:
            session_runtime.resume_sessions(saved["gateway"]["sessions"], actor)
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
                            "max_active": MAX_ACTIVE_SESSIONS,
                            "input_manifest": input_versions.manifest,
                        }
                    )
                    + "\n"
                )
        store.update(status="running")
        try:
            dashboard = DashboardService(root)
            if dashboard.start():
                print(f"Research dashboard: {dashboard.url}", flush=True)
            else:
                print(f"Research dashboard unavailable: {dashboard.url}", flush=True)
        except Exception as error:  # noqa: BLE001 - dashboard is optional
            print(f"Research dashboard unavailable: {type(error).__name__}", flush=True)
            if dashboard is not None:
                dashboard.stop()
            dashboard = None
        with httpx.Client(trust_env=False, 
            base_url=endpoint, headers={"Authorization": "Bearer " + token}, timeout=90
        ) as client:
            # Both readiness checks are one CPU job each; submitting them here saves the
            # Agent the first turn of a window and cannot fail the launch.
            checks = submit_startup_checks(
                client, result_workspace(), os.environ.get("A3_STARTUP_DATASET")
            )
            record_startup_checks(run, checks)
            for job in checks.get("jobs") or []:
                print(f"Startup check {job['kind']}: {job['job_id']}", flush=True)
            for error in checks.get("errors") or []:
                print(f"Startup check failed: {error}", flush=True)
            supervisor = RunSupervisor(
                run=run,
                log_directory=debug_directory(run),
                workspace=result_workspace(),
                sessions=lambda: supervised_sessions(client),
                retain=retain_research,
                result_workspace=result_workspace,
                input_manifest=lambda: input_versions.manifest,
                on_finishing=app.state.begin_shutdown,
            )
            supervisor.run()
    except KeyboardInterrupt:
        # A window-boundary or operator SIGINT is an explicit stop. Ignore a
        # second SIGINT while checkpoints are flushed on the way out.
        interrupted = True
        previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
    finally:
        if dashboard is not None:
            dashboard.stop()
        app.state.begin_shutdown()
        cleanup_errors = []
        try:
            try:
                session_runtime.suspend()
            except (OSError, ValueError, RuntimeError):
                cleanup_errors.append("research suspension")
            app.state.save_checkpoint()
            if interrupted:
                try:
                    exit_code = suspend_interrupted(supervisor, store)
                except OSError:
                    # The process still stops cleanly; the missing observation is
                    # reported instead of replacing the one-line stop notice with a traceback.
                    cleanup_errors.append("supervision state")
                    exit_code = SIGINT_EXIT_CODE
            else:
                store.update(status="suspended")
            if cleanup_errors:
                (run / "cleanup-errors.json").write_text(json.dumps(cleanup_errors))
                print("Cleanup requires review: " + ", ".join(cleanup_errors), flush=True)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)
    if interrupted:
        print(
            f"Run suspended by SIGINT; checkpoint saved under {run}; exit code {exit_code}",
            flush=True,
        )
        raise SystemExit(exit_code)
