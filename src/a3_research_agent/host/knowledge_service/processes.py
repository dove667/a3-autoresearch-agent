"""Manage readiness and shutdown of owned local service processes."""

import socket
import subprocess
import time

import httpx


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def stop_process(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def wait_ready(endpoint, token, process, timeout=120):
    deadline = time.monotonic() + timeout
    with httpx.Client(trust_env=False, timeout=1) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    "Local embedding process exited before readiness; inspect memory logs"
                )
            try:
                response = client.get(
                    endpoint + "/health", headers={"Authorization": "Bearer " + token}
                )
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    raise TimeoutError("Local embedding process did not become ready")
