"""Local-process Harness fixture; does not claim Docker isolation."""

import base64
import os
import subprocess
import sys
import threading

from a3_research_agent.host.sessions.containers import CandidateRuntime


class ProcessSandbox:
    def __init__(self, root, versions):
        self.root, self.versions = root, versions
        self.lock = threading.RLock()
        self.records, self.tokens, self.launches = {}, {}, {}
        self.endpoint = None
        self.support = root / "process-support"
        self.support.mkdir(parents=True)
        (self.support / "sitecustomize.py").write_text(
            "import os, pathlib\n"
            "OriginalPath = pathlib.Path\n"
            "class WorkspacePath(type(OriginalPath())):\n"
            "    def __new__(cls, *parts, **kwargs):\n"
            "        if parts and str(parts[0]) == '/workspace':\n"
            "            parts = (os.getcwd(), *parts[1:])\n"
            "        return super().__new__(cls, *parts, **kwargs)\n"
            "pathlib.Path = WorkspacePath\n"
        )

    def create_version(self, owner, command, token, version_id):
        files = self.versions.read_snapshot(self.versions.root, version_id)
        directory = self.root / owner / "workspace"
        directory.mkdir(parents=True)
        for name, text in files.items():
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        self.tokens[owner] = token
        self.launches[owner] = files
        output = (directory / "process-output.log").open("wb")
        environment = {
            "PATH": os.defpath,
            "PYTHONPATH": str(self.support),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "A3_GATEWAY_URL": self.endpoint,
            "A3_GATEWAY_TOKEN": token,
            "CONTEXT_WINDOW_ESTIMATE": "1000000",
            "CONTEXT_RESERVE_ESTIMATE": "10000",
        }
        try:
            process = subprocess.Popen(
                [sys.executable, *command[1:]],
                cwd=directory,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        finally:
            output.close()
        self.records[owner] = {"owner": owner, "directory": directory, "process": process}
        return {"session_id": owner, "version_id": version_id, "status": "created"}

    def owned(self, owner, session):
        if owner != session or session not in self.records:
            raise PermissionError("Test process is not caller-owned")
        return self.records[session]

    read_workspace_files = CandidateRuntime.read_workspace_files

    def status(self, owner, session):
        assert owner == session
        if session == "main":
            return {"session_id": session, "status": "running"}
        record = self.records[session]
        code = record["process"].poll()
        return {
            "session_id": session,
            "status": "running" if code is None else "exited",
            "exit_code": code,
        }

    def artifact(self, owner, session, name, offset=0, limit=None):
        assert owner == session
        root = self.records[session]["directory"]
        target = (root / name).resolve()
        if root not in target.parents:
            raise PermissionError("Outside test session")
        raw = target.read_bytes()
        selected = raw[offset:] if limit is None else raw[offset : offset + limit]
        return {"path": name, "encoding": "base64", "data": base64.b64encode(selected).decode()}

    def close(self):
        for record in self.records.values():
            process = record["process"]
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
