"""Keep the shared knowledge service alive only during one research launch."""

import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from a3_research_agent.host.runs.directories import debug_directory

from .model import model_path, verify_model
from .processes import wait_ready

SHUTDOWN_GRACE_SECONDS = 5


def group_exists(identifier):
    try:
        os.killpg(identifier, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        observed = subprocess.run(
            ["ps", "-axo", "pgid=,stat="], capture_output=True, text=True, check=False, timeout=5
        )
        if observed.returncode:
            raise RuntimeError("Cannot verify local knowledge process-group shutdown") from None
        for line in observed.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0] == str(identifier) and not fields[1].startswith("Z"):
                return True
        return False
    return True


def signal_group(identifier, selected_signal):
    try:
        os.killpg(identifier, selected_signal)
    except ProcessLookupError:
        pass
    except PermissionError:
        if group_exists(identifier):
            raise


class KnowledgeService:
    """Own a private service process group and always reclaim it on context exit."""

    def __init__(self, project: Path, run: Path, run_id: str):
        self.project, self.run, self.run_id = Path(project), Path(run), run_id
        self.process = None
        self.log = None
        self.token = secrets.token_urlsafe(32)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.endpoint = f"http://127.0.0.1:{self.port}"

    @property
    def environment(self):
        return {
            "A3_KNOWLEDGE_SERVICE_URL": self.endpoint,
            "A3_KNOWLEDGE_SERVICE_TOKEN": self.token,
            "A3_RUN_ID": self.run_id,
        }

    def __enter__(self):
        verify_model(model_path(self.project))
        if not shutil.which(os.environ.get("A3_EMBEDDING_SERVER", "llama-server")):
            raise RuntimeError(
                "Install llama.cpp or configure A3_EMBEDDING_SERVER for semantic retrieval"
            )
        log_path = debug_directory(self.run) / "knowledge-service.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = log_path.open("ab", buffering=0)
        environment = {
            **os.environ,
            **self.environment,
            "A3_KNOWLEDGE_PROJECT": str(self.project),
            "A3_KNOWLEDGE_PARENT_PID": str(os.getpid()),
            "A3_KNOWLEDGE_SERVICE_PORT": str(self.port),
        }
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "a3_research_agent.host.knowledge_service.worker"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=self.log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            wait_ready(self.endpoint, self.token, self.process, timeout=180)
        except BaseException:
            self.close()
            raise
        return self

    def close(self):
        try:
            if self.process is not None:
                # The worker and its llama subprocess share this owned process group.
                self.process.poll()
                signal_group(self.process.pid, signal.SIGTERM)
                deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
                while time.monotonic() < deadline:
                    self.process.poll()
                    if not group_exists(self.process.pid):
                        break
                    time.sleep(0.05)
                if group_exists(self.process.pid):
                    signal_group(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        finally:
            if self.log is not None:
                self.log.close()
                self.log = None
            self.process = None

    def __exit__(self, exc_type, exc, traceback):
        self.close()
