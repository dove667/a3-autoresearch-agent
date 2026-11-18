"""Launch the local, read-only research dashboard."""

import argparse
import os
import threading
import webbrowser
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from a3_research_agent.project import checkout_root, resolve_project_root

from .server import create_app

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main():
    parser = argparse.ArgumentParser(description="Open the A3 research dashboard in a browser.")
    parser.add_argument("--project-root", type=Path, help="Infrastructure project directory")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback host")
    parser.add_argument("--port", type=int, default=8787, help="Local HTTP port")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in a browser")
    args = parser.parse_args()
    try:
        root = resolve_project_root(args.project_root or checkout_root())
    except ValueError as error:
        parser.error(str(error))
    if args.host not in LOOPBACK:
        parser.error("The dashboard only listens on a loopback host")
    load_dotenv(os.environ.get("A3_RSI_ENV_FILE", root / ".env"), override=False)
    url = f"http://{args.host}:{args.port}"
    print(f"A3 研究台  {url}", flush=True)
    print("只读面板，每 5 秒刷新；Ctrl+C 停止", flush=True)
    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    from .live import JobStatusFeed

    uvicorn.run(
        create_app(root, job_status=JobStatusFeed(root=root).statuses),
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
