"""Serve the read-only research dashboard on the local loopback interface."""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .runs import select_run
from .snapshot import build_snapshot


def create_app(project_root, *, process_observer=None):
    """Build one dashboard app; every endpoint is a read-only observation."""
    root = Path(project_root).resolve()
    static = Path(__file__).with_name("static")
    app = FastAPI(
        title="A3 Research Dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def no_store(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return FileResponse(static / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/api/health")
    def health():
        return {"ok": True, "dashboard": "a3-research"}

    @app.get("/api/snapshot")
    def snapshot(run: str = "latest"):
        if run not in {"", "latest"} and select_run(root, run) is None:
            raise HTTPException(status_code=404, detail="unknown run")
        try:
            return build_snapshot(root, run or "latest", process_observer=process_observer)
        except Exception as error:
            raise HTTPException(
                status_code=500,
                detail=f"snapshot unavailable: {type(error).__name__}",
            ) from error

    app.mount("/static", StaticFiles(directory=static), name="static")
    return app
