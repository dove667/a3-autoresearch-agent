"""Run the read-only dashboard for exactly one research Host lifetime."""

import os
import socket
import threading
import time

import uvicorn

from .server import create_app

DEFAULT_PORT = 8787


def configured_port():
    try:
        port = int(os.environ.get("A3_DASHBOARD_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


class DashboardService:
    """Serve the dashboard in a daemon thread and stop it with the Host."""

    def __init__(self, project_root, *, host="127.0.0.1", port=None):
        self.project_root = project_root
        self.host = host
        self.port = configured_port() if port is None else port
        self.url = f"http://{host}:{self.port}"
        self.server = None
        self.thread = None

    def start(self):
        if not port_available(self.host, self.port):
            return False
        config = uvicorn.Config(
            create_app(self.project_root),
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="a3-dashboard", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.server.started:
                return True
            if not self.thread.is_alive():
                return False
            time.sleep(0.05)
        self.stop()
        return False

    def stop(self):
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=5)
