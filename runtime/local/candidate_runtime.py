"""Create sibling containers with host-fixed mounts, identities and resources."""

import base64
import json
import subprocess
import threading
from pathlib import Path, PurePosixPath

ACTIVE_STATES = {"created", "running", "restarting", "paused"}


def workspace_path(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if (
        not name
        or relative.is_absolute()
        or ".." in relative.parts
        or relative == PurePosixPath(".")
    ):
        raise ValueError("Expected a relative workspace file")
    target = root
    for part in relative.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError("Symbolic link paths are not allowed")
    return target


def write_candidate_files(root: Path, files: dict[str, str]) -> None:
    if len(files) > 128 or sum(len(value.encode()) for value in files.values()) > 20 * 1024 * 1024:
        raise ValueError("Candidate input exceeds 128 files or 20 MiB")
    targets = [(workspace_path(root, name), content) for name, content in files.items()]
    for path, content in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


class CandidateRuntime:
    def __init__(self, *, root: Path, image: str, inputs: Path, public_a3: Path, endpoint: str):
        self.root, self.image, self.inputs = root, image, inputs
        self.public_a3, self.endpoint = public_a3, endpoint
        self.records: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def docker(*arguments: str, check: bool = True) -> str:
        result = subprocess.run(
            ["docker", *arguments], capture_output=True, text=True, timeout=30, check=False
        )
        if check and result.returncode:
            raise RuntimeError("Candidate container operation failed")
        return result.stdout + result.stderr if arguments[0] == "logs" else result.stdout

    def owned(self, owner: str, session_id: str) -> dict:
        record = self.records.get(session_id)
        if record is None or record["owner"] != owner:
            raise PermissionError("Candidate session is not accessible")
        return record

    def state(self, record: dict) -> dict:
        return json.loads(
            self.docker("inspect", "--format", "{{json .State}}", record["container_id"])
        )

    def create(self, owner: str, command: list[str], files: dict[str, str], token: str) -> dict:
        with self.lock:
            active = sum(
                self.state(record)["Status"] in ACTIVE_STATES
                for record in self.records.values()
                if not record.get("closed")
            )
            if active >= 2:
                raise RuntimeError("Candidate concurrency resources are busy")
            session_id = owner
            directory = self.root / session_id
            workspace = directory / "workspace"
            workspace.mkdir(parents=True, exist_ok=False)
            write_candidate_files(workspace, files)
            environment = {
                "A3_GATEWAY_URL": self.endpoint,
                "A3_GATEWAY_TOKEN": token,
                "OPENAI_API_BASE": self.endpoint + "/v1",
                "OPENAI_API_KEY": token,
                "MSWEA_COST_TRACKING": "ignore_errors",
                "A3_PROJECT_ROOT": "/opt/a3",
                "A3_HARNESS_ROOT": "/workspace",
                "A3_RUN_DIR": "/workspace/artifacts",
                "A3_GOAL_FILE": "/run/input/goal.md",
                "A3_ACCEPTANCE_FILE": "/run/input/acceptance.md",
                "A3_DEPLOYMENT_FILE": "/run/input/deployment.json",
            }
            arguments = [
                "create",
                "--name",
                "a3-candidate-" + session_id,
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=256",
                "--memory=2g",
                "--cpus=2",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=512m",
                "--log-driver=local",
                "--log-opt=max-size=1m",
                "--log-opt=max-file=2",
                "--mount",
                f"type=bind,src={workspace},dst=/workspace",
                "--mount",
                f"type=bind,src={self.inputs},dst=/run/input,readonly",
                "--mount",
                f"type=bind,src={self.public_a3},dst=/opt/a3,readonly",
                "--workdir=/workspace",
            ]
            for key, value in environment.items():
                arguments.extend(["--env", f"{key}={value}"])
            container = self.docker(*arguments, self.image, *command).strip()
            record = {
                "owner": owner,
                "session_id": session_id,
                "container_id": container,
                "command": command,
                "closed": False,
            }
            self.records[session_id] = record
            (directory / "session.json").write_text(json.dumps(record, indent=2))
            try:
                self.docker("start", container)
            except Exception:
                self.cancel(owner, session_id)
                raise
            return {"session_id": session_id, "status": "created"}

    def status(self, owner: str, session_id: str) -> dict:
        with self.lock:
            record = self.owned(owner, session_id)
            if record["closed"]:
                return {"session_id": session_id, "status": "closed"}
            state = self.state(record)
            output = self.docker("logs", "--tail=200", record["container_id"])[-65536:]
            return {
                "session_id": session_id,
                "status": state["Status"],
                "exit_code": state["ExitCode"],
                "output": output,
            }

    def artifact(self, owner: str, session_id: str, name: str) -> dict:
        self.owned(owner, session_id)
        path = workspace_path(self.root / session_id / "workspace", name)
        if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Artifact must be a file of at most 32 MiB")
        return {
            "path": name,
            "encoding": "base64",
            "data": base64.b64encode(path.read_bytes()).decode(),
        }

    def cancel(self, owner: str, session_id: str) -> dict:
        with self.lock:
            record = self.owned(owner, session_id)
            if not record["closed"]:
                output = self.docker("logs", record["container_id"], check=False)
                (self.root / session_id / "console.log").write_text(output)
                self.docker("rm", "-f", record["container_id"])
                record["closed"] = True
                (self.root / session_id / "session.json").write_text(json.dumps(record, indent=2))
            return {"session_id": session_id, "status": "closed"}

    def close(self) -> None:
        errors = []
        with self.lock:
            for session_id, record in list(self.records.items()):
                try:
                    self.cancel(record["owner"], session_id)
                except Exception:  # noqa: BLE001 — attempt all containers before reporting
                    errors.append(session_id)
        if errors:
            raise RuntimeError("Candidate cleanup pending: " + ", ".join(errors))
