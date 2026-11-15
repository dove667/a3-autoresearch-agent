"""Serve authenticated research memory and own its local embedding subprocess."""

import contextlib
import os
import secrets
import shutil
import subprocess
import threading
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request

from a3_research_agent.harness.memory import dispatch, warm_index
from a3_research_agent.harness.memory.sources import MemorySources

from .embedding import MODEL_ID, LlamaEmbedder
from .model import model_path, verify_model
from .processes import free_port, stop_process, wait_ready


def embedding_command(binary, model, port):
    return [
        binary,
        "--model",
        str(model),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--embedding",
        "--pooling",
        "last",
        "--ctx-size",
        "4096",
        "--batch-size",
        "4096",
        "--ubatch-size",
        "4096",
        "--parallel",
        "1",
        "--threads",
        "2",
        "--threads-batch",
        "2",
        "--no-webui",
    ]


def create_memory_app(project, run_id, token, embedder):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    sources = MemorySources(project, run_id)

    def authorize(authorization: str = Header(default="")):
        if not secrets.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(status_code=401, detail="Memory service token required")

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        from starlette.responses import JSONResponse

        length = request.headers.get("content-length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                return JSONResponse({"error": "invalid_content_length"}, status_code=400)
            if declared < 0:
                return JSONResponse({"error": "invalid_content_length"}, status_code=400)
            if declared > 65536:
                return JSONResponse({"error": "memory_request_too_large"}, status_code=413)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 65536:
                return JSONResponse({"error": "memory_request_too_large"}, status_code=413)
            body.extend(chunk)
        # Starlette's cached request forwards these exact bounded bytes to the route parser.
        request._body = bytes(body)
        return await call_next(request)

    @app.get("/health", dependencies=[Depends(authorize)])
    def health():
        return {"status": "ready", "run_id": run_id, "model_id": MODEL_ID}

    @app.post("/memory", dependencies=[Depends(authorize)])
    def memory(arguments: dict):
        return dispatch(
            arguments,
            root=project / "memory",
            run_id=run_id,
            embedder=embedder,
            model_id=MODEL_ID,
            source_reader=sources,
        )

    return app


def main():
    project = Path(os.environ["A3_MEMORY_PROJECT"]).resolve()
    run_id = os.environ["A3_RUN_ID"]
    token = os.environ["A3_MEMORY_SERVICE_TOKEN"]
    port = int(os.environ["A3_MEMORY_SERVICE_PORT"])
    parent = int(os.environ["A3_MEMORY_PARENT_PID"])
    model = verify_model(model_path(project))
    binary = shutil.which(os.environ.get("A3_MEMORY_LLAMA_SERVER", "llama-server"))
    if binary is None:
        raise RuntimeError(
            "Install llama.cpp or configure A3_MEMORY_LLAMA_SERVER for semantic memory"
        )
    embedding_token = secrets.token_urlsafe(32)
    embedding_port = free_port()
    environment = {**os.environ, "LLAMA_API_KEY": embedding_token}
    process = subprocess.Popen(embedding_command(binary, model, embedding_port), env=environment)
    server = None
    finished = threading.Event()

    def monitor_parent():
        while not finished.wait(0.25):
            if os.getppid() != parent:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    stop_process(process)
                # Parent cannot collect this service after an abrupt exit.
                os._exit(0)

    threading.Thread(target=monitor_parent, daemon=True).start()
    try:
        endpoint = f"http://127.0.0.1:{embedding_port}"
        wait_ready(endpoint, embedding_token, process)
        embedder = LlamaEmbedder(endpoint, embedding_token)
        warm_index(project / "memory", embedder, MODEL_ID)
        app = create_memory_app(project, run_id, token, embedder)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
        )
        server.run()
    finally:
        finished.set()
        stop_process(process)


if __name__ == "__main__":
    main()
