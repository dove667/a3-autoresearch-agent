"""Record request lifecycle and safe error categories without request contents."""

import contextvars
import json
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI

CURRENT_REQUEST = contextvars.ContextVar("gateway_request", default=None)


class GatewayDiagnostics:
    def __init__(self, path: Path, observe=None):
        self.path = path
        self.observe = observe
        self.lock = threading.Lock()

    def write(self, event: dict) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as stream:
                stream.write(json.dumps({"time": time.time(), **event}) + "\n")

    def error(self, layer: str, error: Exception) -> None:
        request = CURRENT_REQUEST.get()
        if request is not None:
            request.update(error_layer=layer, error_type=type(error).__name__)
            status = getattr(error, "status_code", None)
            if type(status) is int:
                request["upstream_status"] = status

    def attach(self, app: FastAPI) -> None:
        @app.middleware("http")
        async def record_request(request, call_next):
            started = time.monotonic()
            entry = {"request_id": uuid.uuid4().hex, "attempt": 1, "method": request.method}
            area = request.url.path.split("/")[1]
            entry["request_kind"] = {
                "v1": "model",
                "context": "model_context",
                "web": "research_web",
                "jobs": "remote_job",
                "sessions": "candidate",
                "versions": "candidate_version",
                "checks": "comparison",
                "fixtures": "isolation_fixture",
                "health": "health",
            }.get(area, "unmatched")
            context = CURRENT_REQUEST.set(entry)
            self.write({**entry, "event": "started"})
            status = 500
            try:
                response = await call_next(request)
                status = response.status_code
                response.headers["X-Request-ID"] = entry["request_id"]
                return response
            finally:
                route = request.scope.get("route")
                finished = {
                    **entry,
                    "event": "finished",
                    "status_code": status,
                    "route": getattr(route, "path", "unmatched"),
                    "wall_seconds": time.monotonic() - started,
                }
                self.write(finished)
                if self.observe is not None:
                    self.observe(finished)
                CURRENT_REQUEST.reset(context)
