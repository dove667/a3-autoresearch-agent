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
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    log_lock = threading.Lock()

    def owner(authorization: str | None) -> str:
        token = (authorization or "").removeprefix("Bearer ")
        for expected, experiment in sessions.items():
            if secrets.compare_digest(token, expected):
                return experiment
        raise HTTPException(401, "Invalid experiment token")

    def remote(operation: str, payload: dict) -> dict:
        try:
            result = remote_call(operation, payload)
        except Exception:  # noqa: BLE001 — never return provider or SSH credential details
            raise HTTPException(502, "Remote controller unavailable") from None
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @app.get("/health")
    def health():
        return {"status": "ready"}

    @app.post("/v1/responses")
    def responses(body: dict, authorization: Annotated[str | None, Header()] = None):
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
        experiment = owner(authorization)
        return remote("submit", body.model_dump() | {"experiment_id": experiment})

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
