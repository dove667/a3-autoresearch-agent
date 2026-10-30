"""Host-owned forwarding boundary for model requests and experiment jobs."""

import json
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: list[str] = Field(min_length=1, max_length=256)
    gpu: StrictBool = False
    files: dict[str, str] = Field(default_factory=dict)


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: list[str] = Field(min_length=1, max_length=256)
    files: dict[str, str] = Field(default_factory=dict)


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
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    log_lock = threading.Lock()
    session_lock = threading.RLock()
    children: dict[str, dict] = {}
    scope_parents: dict[str, str] = {}
    jobs: dict[str, set[str]] = {}
    root_owners = set(sessions.values())
    app.state.closing = False

    def session_event(event, session_id):
        record = children[session_id]
        entry = {
            "event": event,
            "session_id": session_id,
            "parent": record["parent"],
            "time": time.time(),
        }
        with log_lock, usage_log.with_name("session-events.jsonl").open("a") as stream:
            stream.write(json.dumps(entry) + "\n")

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
        with session_lock:
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

    app.state.begin_shutdown = begin_shutdown
    app.state.close_resources = close_resources

    def owner(authorization: str | None) -> str:
        token = (authorization or "").removeprefix("Bearer ")
        for expected, experiment in tuple(sessions.items()):
            if secrets.compare_digest(token, expected):
                return experiment
        raise HTTPException(401, "Invalid experiment token")

    def deployment_call(operation, *args):
        try:
            return operation(*args)
        except PermissionError:
            raise HTTPException(403, "Resource is not accessible") from None
        except ValueError:
            raise HTTPException(422, "Invalid workspace file or input size") from None
        except Exception:  # noqa: BLE001 — do not expose Docker or transport details
            raise HTTPException(
                409, "Deployment operation failed; check available resources"
            ) from None

    def child(authorization, session_id):
        record = children.get(session_id)
        if record is None or not manages(owner(authorization), record):
            raise HTTPException(403, "Candidate session is not accessible")
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
                result = deployment_call(sandbox.create, scope, body.command, body.files, token)
            except Exception:
                sessions.pop(token, None)
                raise
            children[result["session_id"]] = {
                "parent": parent,
                "owner": scope,
                "token": token,
                "closed": False,
            }
            scope_parents[scope] = parent
            session_event("created", result["session_id"])
            return result

    @app.get("/sessions")
    def list_sessions(authorization: Annotated[str | None, Header()] = None):
        experiment = owner(authorization)
        with session_lock:
            return {
                "sessions": [
                    {
                        "session_id": candidate,
                        "parent": record["parent"],
                        "closed": record["closed"],
                    }
                    for candidate, record in children.items()
                    if manages(experiment, record)
                ]
            }

    @app.get("/sessions/{session_id}")
    def session_status(session_id: str, authorization: Annotated[str | None, Header()] = None):
        record = child(authorization, session_id)
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
        session_id: str, path: str, authorization: Annotated[str | None, Header()] = None
    ):
        record = child(authorization, session_id)
        return deployment_call(sandbox.artifact, record["owner"], session_id, path)

    @app.post("/fixtures/isolation")
    def create_fixture(authorization: Annotated[str | None, Header()] = None):
        with session_lock:
            accepting_resources()
            experiment = owner(authorization)
            if fixtures is None:
                raise HTTPException(503, "Isolation fixtures are not configured")
            return deployment_call(fixtures.create, experiment)

    @app.get("/fixtures/isolation/{fixture_id}")
    def verify_fixture(fixture_id: str, authorization: Annotated[str | None, Header()] = None):
        return deployment_call(fixtures.verify, owner(authorization), fixture_id)

    @app.delete("/fixtures/isolation/{fixture_id}")
    def cancel_fixture(fixture_id: str, authorization: Annotated[str | None, Header()] = None):
        return deployment_call(fixtures.cancel, owner(authorization), fixture_id)

    def remote(operation: str, payload: dict) -> dict:
        try:
            result = remote_call(operation, payload)
        except Exception:  # noqa: BLE001 — never return provider or SSH credential details
            raise HTTPException(502, "Remote controller unavailable") from None
        if result.get("status") == "error":
            raise HTTPException(400, result["error"])
        return result

    @app.get("/health")
    def health():
        return {"status": "ready"}

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
        if isinstance(arguments.get("input"), str):
            arguments["input"] = [{"role": "user", "content": arguments["input"]}]
        started = time.monotonic()
        try:
            response = forward_model(model=model_name, **arguments)
            data = response.model_dump(mode="json") if hasattr(response, "model_dump") else response
        except Exception:  # noqa: BLE001 — never return provider or SSH credential details
            raise HTTPException(502, "Model forwarding failed") from None
        entry = {
            "experiment_id": experiment,
            "model": model_name,
            "wall_seconds": time.monotonic() - started,
            "usage": data.get("usage"),
        }
        with log_lock:
            usage_log.parent.mkdir(parents=True, exist_ok=True)
            with usage_log.open("a") as stream:
                stream.write(json.dumps(entry) + "\n")
        return data

    @app.post("/jobs")
    def submit(body: JobRequest, authorization: Annotated[str | None, Header()] = None):
        with session_lock:
            accepting_resources()
            experiment = owner(authorization)
            result = remote("submit", body.model_dump() | {"experiment_id": experiment})
            if result.get("job_id"):
                jobs.setdefault(experiment, set()).add(result["job_id"])
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
