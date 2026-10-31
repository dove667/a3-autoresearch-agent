"""Host-owned forwarding boundary for model requests and experiment jobs."""

import hashlib
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from runtime.local.candidate_runtime import (
    DEFAULT_ARTIFACT_PAGE_BYTES,
    MAX_ACTIVE_CANDIDATES,
    MAX_ARTIFACT_BYTES,
    ArtifactTooLargeError,
    CandidateCapacityError,
)
from runtime.local.comparison_checks import check_comparison
from runtime.local.evolution import EvolutionState
from runtime.local.gateway_diagnostics import CURRENT_REQUEST, GatewayDiagnostics
from runtime.local.input_versions import InputVersions
from runtime.local.run_state import RunStateStore


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: list[str] = Field(min_length=1, max_length=256)
    gpu: StrictBool = False
    files: dict[str, str] = Field(default_factory=dict)


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: list[str] = Field(min_length=1, max_length=256)
    files: dict[str, str] = Field(default_factory=dict)
    version_id: str | None = None
    parent_version: str | None = None


class TrialRequest(SessionRequest):
    baseline_version: str
    checklist: str = Field(min_length=1, max_length=32000)


class TrialDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retain: StrictBool
    reason: str = Field(min_length=1, max_length=16000)
    evidence: list[str] = Field(default_factory=list, max_length=128)


class SessionStopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=4000)


class InputUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str = Field(pattern=r"^[0-9a-f]{64}$")


class VersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: dict[str, str]
    parent_version: str | None = None


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline: dict
    candidate: dict


MODEL_FIELDS = {
    "model",
    "input",
    "instructions",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "reasoning",
    "text",
    "max_output_tokens",
    "temperature",
    "top_p",
    "store",
    "stream",
}


def create_app(
    *,
    model_name: str,
    sessions: dict[str, str],
    forward_model: Callable,
    remote_call: Callable,
    usage_log: Path,
    sandbox=None,
    fixtures=None,
    versions=None,
    initial_inputs: Path | None = None,
    main_owner: str | None = None,
    start_main_session: bool = False,
    common_instructions: str = "",
    state_store: RunStateStore | None = None,
    restored_state: dict | None = None,
    input_versions: InputVersions | None = None,
    operator_token: str | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        # Poll independently of model calls and Agent status requests, including during H0.
        stopped = threading.Event()

        def reap_workers():
            while not stopped.wait(2):
                try:
                    sandbox.reap_exited()
                except Exception as error:  # noqa: BLE001 — retry after Host storage/daemon failures
                    logging.getLogger(__name__).error(
                        "Worker container cleanup will retry (%s)", type(error).__name__
                    )

        reaper = None
        if sandbox is not None and hasattr(sandbox, "reap_exited"):
            reaper = threading.Thread(
                target=reap_workers, name="worker-container-reaper", daemon=True
            )
            reaper.start()
        try:
            yield
        finally:
            stopped.set()
            if reaper is not None:
                reaper.join()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    diagnostics = GatewayDiagnostics(usage_log.with_name("gateway-events.jsonl"))
    diagnostics.attach(app)
    log_lock = threading.Lock()
    session_lock = threading.RLock()
    children: dict[str, dict] = {}
    scope_parents: dict[str, str] = {}
    jobs: dict[str, set[str]] = {}
    root_owners = set(sessions.values())
    app.state.closing = False
    evolution = (
        EvolutionState(usage_log.with_name("evolution.json"), main_owner) if main_owner else None
    )
    if restored_state is not None:
        sessions.clear()
        sessions.update(restored_state["sessions"])
        root_owners = set(restored_state["root_owners"])
        children.update(restored_state["children"])
        # A process stop may finish just before the Gateway checkpoint is written.
        runtime_records = getattr(sandbox, "records", {})
        for session_id, record in children.items():
            if record.get("self_stopped") or runtime_records.get(session_id, {}).get(
                "self_stopped"
            ):
                record["self_stopped"] = True
                sessions.pop(record["token"], None)
        scope_parents.update(restored_state["scope_parents"])
        jobs.update({key: set(value) for key, value in restored_state["jobs"].items()})
        if evolution and restored_state.get("evolution"):
            saved = restored_state["evolution"]
            evolution.main_owner = saved["main_owner"]
            evolution.trials = saved["trials"]
            evolution.transitions = saved["transitions"]
        if fixtures is not None:
            fixtures.records = restored_state.get("fixtures", {})

    def save_checkpoint():
        with session_lock:
            if state_store is not None:
                state_store.update(
                    gateway={
                        "sessions": dict(sessions),
                        "root_owners": sorted(root_owners),
                        "children": children,
                        "scope_parents": scope_parents,
                        "jobs": {key: sorted(value) for key, value in jobs.items()},
                        "evolution": evolution.snapshot() if evolution else None,
                        "fixtures": fixtures.records if fixtures is not None else {},
                    }
                )

    app.state.save_checkpoint = save_checkpoint

    def result_location():
        with session_lock:
            actor = evolution.main_owner if evolution else main_owner
            record = children.get(actor, {})
            return actor, record.get("version_id")

    app.state.result_location = result_location

    def session_event(event, session_id):
        record = children[session_id]
        entry = {
            "event": event,
            "session_id": session_id,
            "parent": record["parent"],
            "time": time.time(),
            "version_id": record.get("version_id"),
        }
        with log_lock, usage_log.with_name("session-events.jsonl").open("a") as stream:
            stream.write(json.dumps(entry) + "\n")
        save_checkpoint()

    def descendants(session_id):
        by_parent: dict[str, list[str]] = {}
        for candidate, record in children.items():
            by_parent.setdefault(record["parent"], []).append(candidate)
        branch, pending = [], [session_id]
        while pending:
            candidate = pending.pop()
            branch.append(candidate)
            pending.extend(by_parent.get(children[candidate]["owner"], []))
        return branch

    def manages(experiment, record):
        ancestor = record["parent"]
        while ancestor:
            if ancestor == experiment:
                return True
            ancestor = scope_parents.get(ancestor)
        return False

    def accepting_resources():
        if app.state.closing:
            raise HTTPException(503, "Deployment is shutting down")

    def begin_shutdown():
        with input_versions.lock if input_versions else nullcontext(), session_lock:
            app.state.closing = True

    def close_owner_resources(experiment):
        errors = []
        if fixtures is not None:
            try:
                fixtures.close_owner(experiment)
            except Exception:  # noqa: BLE001 — continue independent cleanup operations
                errors.append("isolation fixtures")
        for job_id in list(jobs.get(experiment, set())):
            try:
                remote("cancel", {"experiment_id": experiment, "job_id": job_id})
                jobs[experiment].discard(job_id)
            except Exception:  # noqa: BLE001 — keep failed jobs available for cleanup retry
                errors.append("remote job " + job_id)
        return errors

    def close_child(session_id):
        with session_lock:
            errors = []
            for candidate in reversed(descendants(session_id)):
                record = children[candidate]
                if record["closed"]:
                    continue
                sessions.pop(record["token"], None)
                candidate_errors = close_owner_resources(record["owner"])
                try:
                    sandbox.cancel(record["owner"], candidate)
                except Exception:  # noqa: BLE001 — retain ancestor management for retry
                    candidate_errors.append("candidate " + candidate)
                if not candidate_errors:
                    record["closed"] = True
                    session_event("closed", candidate)
                errors.extend(candidate_errors)
            if errors:
                raise RuntimeError("Cleanup pending: " + ", ".join(errors))
            return {"session_id": session_id, "status": "closed"}

    def close_resources():
        begin_shutdown()
        errors = []
        with session_lock:
            for session_id in list(children):
                try:
                    close_child(session_id)
                except RuntimeError:
                    errors.append(session_id)
            for experiment in root_owners:
                errors.extend(close_owner_resources(experiment))
        if errors:
            raise RuntimeError("Deployment cleanup pending: " + ", ".join(errors))
        save_checkpoint()

    app.state.begin_shutdown = begin_shutdown
    app.state.close_resources = close_resources

    def owner(authorization: str | None) -> str:
        token = (authorization or "").removeprefix("Bearer ")
        for expected, experiment in tuple(sessions.items()):
            if secrets.compare_digest(token, expected):
                request = CURRENT_REQUEST.get()
                if request is not None:
                    request["experiment_id"] = experiment
                return experiment
        raise HTTPException(401, "Invalid experiment token")

    def deployment_call(operation, *args):
        try:
            return operation(*args)
        except PermissionError:
            raise HTTPException(403, "Resource is not accessible") from None
        except ArtifactTooLargeError as error:
            raise HTTPException(
                422,
                {
                    "reason": "artifact_too_large",
                    "size_bytes": error.size_bytes,
                    "max_bytes": MAX_ARTIFACT_BYTES,
                    "retry_query": {"offset": 0, "limit": DEFAULT_ARTIFACT_PAGE_BYTES},
                },
            ) from None
        except ValueError:
            raise HTTPException(422, "Invalid workspace file or input size") from None
        except CandidateCapacityError as error:
            diagnostics.error("candidate_capacity", error)
            raise HTTPException(
                409,
                {
                    "reason": "candidate_capacity",
                    "max_active": MAX_ACTIVE_CANDIDATES,
                    "retry_after_seconds": 2,
                },
            ) from None
        except Exception as error:  # noqa: BLE001 — do not expose Docker or transport details
            diagnostics.error("deployment", error)
            raise HTTPException(
                409, "Deployment operation failed; check available resources"
            ) from None

    def child(authorization, session_id):
        record = children.get(session_id)
        if record is None or not manages(owner(authorization), record):
            raise HTTPException(403, "Candidate session is not accessible")
        return record

    def own_session(authorization):
        experiment = owner(authorization)
        record = children.get(experiment)
        if record is None or record["owner"] != experiment:
            raise HTTPException(403, "This identity has no candidate session")
        return record

    @app.post("/sessions")
    def create_session(body: SessionRequest, authorization: Annotated[str | None, Header()] = None):
        if sandbox is None:
            raise HTTPException(503, "Candidate execution is not configured")
        with session_lock:
            accepting_resources()
            parent = owner(authorization)
            scope = "candidate-" + secrets.token_hex(16)
            token = secrets.token_urlsafe(32)
            sessions[token] = scope
            try:
                if body.version_id and (body.files or body.parent_version):
                    raise HTTPException(422, "Choose version_id or files with parent_version")
                version_id = body.version_id
                if versions is not None:
                    if hasattr(sandbox, "check_capacity"):
                        deployment_call(sandbox.check_capacity)
                    if version_id is None:
                        previous = body.parent_version or children.get(parent, {}).get("version_id")
                        version_id = deployment_call(versions.create, parent, body.files, previous)[
                            "version_id"
                        ]
                    deployment_call(versions.grant, parent, scope, version_id)
                    if initial_inputs is not None and parent in root_owners and not children:
                        result = deployment_call(
                            sandbox.create_initial,
                            scope,
                            body.command,
                            token,
                            version_id,
                            initial_inputs,
                        )
                    else:
                        result = deployment_call(
                            sandbox.create_version, scope, body.command, token, version_id
                        )
                elif version_id:
                    raise HTTPException(503, "Candidate versions are not configured")
                else:
                    result = deployment_call(sandbox.create, scope, body.command, body.files, token)
            except Exception:
                sessions.pop(token, None)
                raise
            children[result["session_id"]] = {
                "parent": parent,
                "owner": scope,
                "token": token,
                "closed": False,
                "version_id": result.get("version_id"),
            }
            scope_parents[scope] = parent
            if evolution and start_main_session and len(children) == 1:
                evolution.main_owner = scope
                evolution.save()
            session_event("created", result["session_id"])
            return result

    def start_local_harness(command: list[str], files: dict[str, str], authorization: str):
        """Import a local package inside the Host; no unbounded HTTP route is exposed."""
        with session_lock:
            accepting_resources()
            parent = owner(authorization)
            if parent not in root_owners:
                raise HTTPException(403, "Local Harness imports require the Host root identity")
            if versions is None or sandbox is None:
                raise HTTPException(503, "Candidate execution is not configured")
            request = SessionRequest(command=command)
            if hasattr(sandbox, "check_capacity"):
                deployment_call(sandbox.check_capacity)
            version = versions.create_local(parent, files)
            request.version_id = version["version_id"]
            return create_session(request, authorization)

    app.state.start_local_harness = start_local_harness

    def evolution_state():
        if evolution is None or versions is None:
            raise HTTPException(503, "Linear evolution is not configured")
        return evolution

    @app.get("/evolution")
    def inspect_evolution(authorization: Annotated[str | None, Header()] = None):
        actor = owner(authorization)
        with session_lock:
            state = evolution_state()
            if actor not in root_owners and actor != state.main_owner and actor not in state.trials:
                raise HTTPException(403, "Evolution state is not accessible")
            if actor in state.trials and actor != state.main_owner:
                return {"main_owner": state.main_owner, "trial": state.trials[actor]}
            return state.snapshot()

    @app.post("/evolution/trials")
    def start_trial(body: TrialRequest, authorization: Annotated[str | None, Header()] = None):
        with session_lock:
            state = evolution_state()
            actor = owner(authorization)
            deployment_call(state.require_main, actor)
            if state.pending():
                raise HTTPException(409, "Decide the current candidate before starting another")
            deployment_call(versions.owned, actor, body.baseline_version)
            request = SessionRequest(**body.model_dump(exclude={"baseline_version", "checklist"}))
            result = create_session(request, authorization)
            state.register(
                result["session_id"], body.baseline_version, result["version_id"], body.checklist
            )
            save_checkpoint()
            return {**result, "role": "candidate_main"}

    @app.post("/evolution/trials/{session_id}/decision")
    def decide_trial(
        session_id: str, body: TrialDecision, authorization: Annotated[str | None, Header()] = None
    ):
        with session_lock:
            state = evolution_state()
            actor = owner(authorization)
            deployment_call(state.require_main, actor)
            trial = state.trials.get(session_id)
            if not trial or trial["status"] != "testing":
                raise HTTPException(409, "No pending trial with this identity")
            record = child(authorization, session_id)
            # The Agent judges the checklist. Host only verifies evidence accessibility.
            if body.retain:
                if not body.evidence:
                    raise HTTPException(422, "Retaining a candidate requires evidence paths")
                status = deployment_call(sandbox.status, record["owner"], session_id)
                if status.get("status") != "running":
                    raise HTTPException(409, "Only a running candidate can take over")
                for path in body.evidence:
                    deployment_call(sandbox.artifact, record["owner"], session_id, path)
            else:
                deployment_call(close_child, session_id)
            result = state.decide(actor, session_id, body.retain, body.reason, body.evidence)
            save_checkpoint()
            return result

    @app.get("/sessions")
    def list_sessions(
        authorization: Annotated[str | None, Header()] = None,
        include_status: bool = False,
    ):
        experiment = owner(authorization)
        with session_lock:
            result = []
            for candidate, record in children.items():
                if not manages(experiment, record):
                    continue
                item = {
                    "session_id": candidate,
                    "parent": record["parent"],
                    "closed": record["closed"],
                    "version_id": record["version_id"],
                }
                if include_status:
                    state = (
                        deployment_call(sandbox.status, record["owner"], candidate)
                        if not record["closed"]
                        else {"status": "closed"}
                    )
                    item.update(status=state.get("status"), exit_code=state.get("exit_code"))
                result.append(item)
            return {"sessions": result}

    @app.post("/versions")
    def save_version(body: VersionRequest, authorization: Annotated[str | None, Header()] = None):
        with session_lock:
            accepting_resources()
            experiment = owner(authorization)
            if versions is None:
                raise HTTPException(503, "Candidate versions are not configured")
            return deployment_call(versions.create, experiment, body.files, body.parent_version)

    @app.get("/versions")
    def list_versions(authorization: Annotated[str | None, Header()] = None):
        experiment = owner(authorization)
        if versions is None:
            raise HTTPException(503, "Candidate versions are not configured")
        return {"versions": versions.list(experiment)}

    @app.post("/checks/comparison")
    def comparison(body: ComparisonRequest, authorization: Annotated[str | None, Header()] = None):
        owner(authorization)
        return check_comparison(body.baseline, body.candidate)

    @app.get("/sessions/self")
    def self_status(authorization: Annotated[str | None, Header()] = None):
        record = own_session(authorization)
        return deployment_call(sandbox.status, record["owner"], record["owner"])

    @app.post("/sessions/self/stop")
    def stop_self(body: SessionStopRequest, authorization: Annotated[str | None, Header()] = None):
        with session_lock:
            record = own_session(authorization)
            session_id = record["owner"]
            # This explicit request is terminal. It never decides acceptance or cancels a branch.
            session_event("self_stop_requested", session_id)
            result = deployment_call(sandbox.stop, session_id, session_id)
            record["self_stopped"] = True
            record["stop_reason"] = body.reason
            sessions.pop(record["token"], None)
            session_event("self_stopped", session_id)
            return result

    @app.get("/sessions/{session_id}")
    def session_status(session_id: str, authorization: Annotated[str | None, Header()] = None):
        record = (
            own_session(authorization)
            if session_id == owner(authorization)
            else child(authorization, session_id)
        )
        return deployment_call(sandbox.status, record["owner"], session_id)

    @app.delete("/sessions/{session_id}")
    def cancel_session(
        session_id: str,
        cascade: bool = False,
        authorization: Annotated[str | None, Header()] = None,
    ):
        with session_lock:
            child(authorization, session_id)
            open_descendants = [
                candidate
                for candidate in descendants(session_id)
                if candidate != session_id and not children[candidate]["closed"]
            ]
            if open_descendants and not cascade:
                raise HTTPException(
                    409, {"reason": "descendants_open", "sessions": open_descendants}
                )
            return deployment_call(close_child, session_id)

    @app.get("/sessions/{session_id}/artifacts")
    def session_artifact(
        session_id: str,
        path: str,
        authorization: Annotated[str | None, Header()] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int | None, Query(ge=1, le=MAX_ARTIFACT_BYTES)] = None,
    ):
        record = child(authorization, session_id)
        if limit is not None or offset:
            return deployment_call(
                sandbox.artifact, record["owner"], session_id, path, offset, limit
            )
        return deployment_call(sandbox.artifact, record["owner"], session_id, path)

    @app.get("/sessions/{session_id}/versions")
    def descendant_versions(session_id: str, authorization: Annotated[str | None, Header()] = None):
        record = child(authorization, session_id)
        if versions is None:
            raise HTTPException(503, "Candidate versions are not configured")
        experiment = owner(authorization)
        with session_lock:
            saved = versions.list(record["owner"])
            for version in saved:
                versions.grant(record["owner"], experiment, version["version_id"])
            return {"versions": saved}

    @app.post("/fixtures/isolation")
    def create_fixture(authorization: Annotated[str | None, Header()] = None):
        with session_lock:
            accepting_resources()
            experiment = owner(authorization)
            if fixtures is None:
                raise HTTPException(503, "Isolation fixtures are not configured")
            result = deployment_call(fixtures.create, experiment)
            save_checkpoint()
            return result

    @app.get("/fixtures/isolation/{fixture_id}")
    def verify_fixture(fixture_id: str, authorization: Annotated[str | None, Header()] = None):
        return deployment_call(fixtures.verify, owner(authorization), fixture_id)

    @app.delete("/fixtures/isolation/{fixture_id}")
    def cancel_fixture(fixture_id: str, authorization: Annotated[str | None, Header()] = None):
        with session_lock:
            result = deployment_call(fixtures.cancel, owner(authorization), fixture_id)
            save_checkpoint()
            return result

    def remote(operation: str, payload: dict) -> dict:
        try:
            result = remote_call(operation, payload)
        except Exception as error:  # noqa: BLE001 — never return provider or SSH credential details
            diagnostics.error("remote_transport", error)
            raise HTTPException(502, "Remote controller unavailable") from None
        if result.get("status") == "error":
            request = CURRENT_REQUEST.get()
            if request is not None:
                request.update(error_layer="remote_controller", error_type="ControllerError")
            raise HTTPException(400, result["error"])
        if result.get("status") == "resource_busy":
            request = CURRENT_REQUEST.get()
            if request is not None:
                request["wait_reason"] = result.get("reason", "remote_capacity")
                request["retry_after_seconds"] = result.get("retry_after_seconds")
        return result

    @app.get("/health")
    def health():
        return {"status": "ready"}

    def require_operator(authorization):
        supplied = (authorization or "").removeprefix("Bearer ")
        if not operator_token or not secrets.compare_digest(supplied, operator_token):
            raise HTTPException(403, "Host operator authorization required")
        if input_versions is None:
            raise HTTPException(503, "Versioned Agent inputs are not configured")

    def prepare_inputs():
        try:
            return input_versions.prepare()
        except (OSError, ValueError) as error:
            raise HTTPException(422, str(error)) from None

    @app.get("/control/inputs")
    def preview_inputs(authorization: Annotated[str | None, Header()] = None):
        require_operator(authorization)
        proposal = prepare_inputs()
        return {
            "current": input_versions.manifest,
            "proposed": {key: proposal[key] for key in ("digest", "files")},
            "warning": "热加载会更新本轮目标、验收和Prompt。已发出的请求仍使用原版本；后续请求使用新版。需确认预览内容摘要后执行。",
        }

    @app.post("/control/inputs")
    def reload_inputs(
        body: InputUpdateRequest, authorization: Annotated[str | None, Header()] = None
    ):
        require_operator(authorization)
        with input_versions.lock:
            accepting_resources()
            proposal = prepare_inputs()
            if not secrets.compare_digest(body.confirm, proposal["digest"]):
                raise HTTPException(409, "Preview changed; inspect and confirm the current digest")
            try:
                return input_versions.activate(proposal, "hot_reload")
            except OSError:
                raise HTTPException(
                    503, "Agent input update failed; inspect Host storage"
                ) from None

    @app.post("/v1/responses")
    def responses(body: dict, authorization: Annotated[str | None, Header()] = None):
        accepting_resources()
        experiment = owner(authorization)
        if set(body) - MODEL_FIELDS:
            raise HTTPException(422, "Unsupported model parameter")
        if body.get("stream"):
            raise HTTPException(422, "Streaming forwarding is not enabled")
        tools = body.get("tools") or []
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict) or tool.get("type") != "function" for tool in tools
        ):
            raise HTTPException(422, "Only client-executed function tools are supported")
        arguments = {key: value for key, value in body.items() if key != "model"}
        arguments.pop("stream", None)
        with session_lock:
            trial = evolution.trials.get(experiment, {}) if evolution else {}
            receives_common = bool(
                (common_instructions or input_versions)
                and evolution
                and (experiment == evolution.main_owner or trial.get("status") == "testing")
            )
        input_manifest, active_common = (
            input_versions.model_instructions()
            if input_versions is not None
            else ({}, common_instructions)
        )
        if receives_common:
            specific = arguments.get("instructions")
            if specific is not None and not isinstance(specific, str):
                raise HTTPException(422, "Instructions must be text")
            arguments["instructions"] = (
                active_common + "\n\n" + specific if specific else active_common
            )
        if isinstance(arguments.get("input"), str):
            arguments["input"] = [{"role": "user", "content": arguments["input"]}]
        started = time.monotonic()
        try:
            response = forward_model(model=model_name, **arguments)
            data = response.model_dump(mode="json") if hasattr(response, "model_dump") else response
        except Exception as error:  # noqa: BLE001 — never return provider or SSH credential details
            diagnostics.error("model_provider", error)
            raise HTTPException(502, "Model forwarding failed") from None
        entry = {
            "experiment_id": experiment,
            "model": model_name,
            "wall_seconds": time.monotonic() - started,
            "usage": data.get("usage"),
        }
        if receives_common:
            entry["common_instructions_sha256"] = hashlib.sha256(active_common.encode()).hexdigest()
        if input_manifest:
            entry["input_revision"] = input_manifest["revision"]
            entry["input_versions"] = input_manifest["files"]
        with log_lock:
            usage_log.parent.mkdir(parents=True, exist_ok=True)
            with usage_log.open("a") as stream:
                stream.write(json.dumps(entry) + "\n")
        if input_manifest:
            from fastapi.responses import JSONResponse

            return JSONResponse(data, headers={"X-A3-Input-Revision": input_manifest["revision"]})
        return data

    @app.post("/jobs")
    def submit(body: JobRequest, authorization: Annotated[str | None, Header()] = None):
        with session_lock:
            accepting_resources()
            experiment = owner(authorization)
            result = remote("submit", body.model_dump() | {"experiment_id": experiment})
            if result.get("job_id"):
                jobs.setdefault(experiment, set()).add(result["job_id"])
                save_checkpoint()
            return result

    @app.get("/jobs/{job_id}")
    def status(job_id: str, authorization: Annotated[str | None, Header()] = None):
        return remote("status", {"experiment_id": owner(authorization), "job_id": job_id})

    @app.delete("/jobs/{job_id}")
    def cancel(job_id: str, authorization: Annotated[str | None, Header()] = None):
        return remote("cancel", {"experiment_id": owner(authorization), "job_id": job_id})

    @app.get("/jobs/{job_id}/artifacts")
    def artifact(job_id: str, path: str, authorization: Annotated[str | None, Header()] = None):
        return remote(
            "artifact",
            {
                "experiment_id": owner(authorization),
                "job_id": job_id,
                "path": path,
            },
        )

    return app
