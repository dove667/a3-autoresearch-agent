"""Create sibling containers with host-fixed mounts, identities and resources."""

import base64
import hashlib
import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath

from a3_research_agent.host.published_harness import (
    COMMAND,
    pinned_resume,
    research_workspace,
    run_commit,
)
from a3_research_agent.workspace.paths import workspace_path

ACTIVE_STATES = {"created", "running", "restarting", "paused"}
MAX_ACTIVE_CANDIDATES = 2
MAX_INPUT_FILES = 128
MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
DEFAULT_ARTIFACT_PAGE_BYTES = 1024 * 1024


class CandidateCapacityError(RuntimeError):
    """No free local candidate slot; callers may poll before retrying."""


class CandidateContainerMissingError(RuntimeError):
    """Docker confirmed that a previously registered container no longer exists."""


class ArtifactTooLargeError(ValueError):
    """The caller must request a bounded page instead of the whole artifact."""

    def __init__(self, size_bytes: int):
        super().__init__("Artifact exceeds the whole-file response limit")
        self.size_bytes = size_bytes


def candidate_workspace(root: Path, session: str) -> Path:
    directory = workspace_path(root, session)
    workspace = directory / "workspace"
    if workspace.is_symlink():
        expected = (
            research_workspace(root.parent, session)
            if run_commit(root.parent)
            else workspace_path(root.parent, f"versions/worktrees/{session}/source")
        )
        if workspace.resolve(strict=True) != expected.resolve(strict=True):
            raise ValueError("Resume workspace must reference its own Host-managed worktree")
        return expected
    return workspace_path(directory, "workspace")


def write_candidate_files(root: Path, files: dict[str, str]) -> None:
    if (
        len(files) > MAX_INPUT_FILES
        or sum(len(value.encode()) for value in files.values()) > MAX_INPUT_BYTES
    ):
        raise ValueError("Candidate input exceeds 128 files or 20 MiB")
    targets = [(workspace_path(root, name), content) for name, content in files.items()]
    for path, content in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


class CandidateRuntime:
    def __init__(
        self,
        *,
        root: Path,
        image: str,
        inputs: Path,
        public_a3: Path,
        endpoint: str,
        versions=None,
        published_source: Path | None = None,
    ):
        self.root, self.image, self.inputs = root, image, inputs
        self.public_a3, self.endpoint = public_a3, endpoint
        self.versions = versions
        self.published_source = published_source
        self.records: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def docker(*arguments: str, check: bool = True) -> str:
        result = subprocess.run(
            ["docker", *arguments], capture_output=True, text=True, timeout=30, check=False
        )
        if check and result.returncode:
            if arguments[0] in {"inspect", "rm"} and any(
                text in result.stderr.lower() for text in ("no such object", "no such container")
            ):
                raise CandidateContainerMissingError("Candidate container no longer exists")
            raise RuntimeError("Candidate container operation failed")
        return result.stdout + result.stderr if arguments[0] == "logs" else result.stdout

    def owned(self, owner: str, session_id: str) -> dict:
        record = self.records.get(session_id)
        if record is None or record["owner"] != owner:
            raise PermissionError("Candidate session is not accessible")
        return record

    def state(self, record: dict) -> dict:
        if record.get("container_removed"):
            return record["terminal_state"]
        return json.loads(
            self.docker("inspect", "--format", "{{json .State}}", record["container_id"])
        )

    def save_record(self, record: dict) -> None:
        self.write_durable(
            self.root / record["owner"] / "session.json", json.dumps(record, indent=2)
        )

    @staticmethod
    def write_durable(path: Path, content: str) -> None:
        """Commit Host evidence before allowing Docker to discard its copy."""
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def archive_and_remove(self, record: dict, state: dict) -> None:
        """Remove only this stopped container, after saving its exit and console."""
        if record.get("container_removed"):
            return
        if state["Status"] not in {"exited", "dead", "created"}:
            raise RuntimeError("Cannot archive a container that may still be executing")
        if not record.get("terminal_state"):
            # All worker files live on the Host bind mount; no workspace copy is needed.
            candidate_workspace(self.root, record["owner"]).resolve(strict=True)
            output = self.docker("logs", record["container_id"])
            self.write_durable(self.root / record["owner"] / "console.log", output)
            record["terminal_state"] = state
            record["exit_saved_at"] = time.time()
        self.save_record(record)
        try:
            self.docker("rm", record["container_id"])
        except CandidateContainerMissingError:
            pass
        record["container_removed"] = True
        record.pop("cleanup_error", None)
        self.save_record(record)

    def collect_exit(self, record: dict) -> dict:
        """Keep exit observable even if archiving or removal needs another attempt."""
        try:
            state = self.state(record)
        except CandidateContainerMissingError:
            if not record.get("terminal_state"):
                raise
            record["container_removed"] = True
            record.pop("cleanup_error", None)
            self.save_record(record)
            return record["terminal_state"]
        if state["Status"] in {"exited", "dead"}:
            try:
                self.archive_and_remove(record, state)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                record["cleanup_error"] = type(error).__name__
                self.save_record(record)
        return state

    def reap_exited(self) -> None:
        """Retry terminal-container cleanup without changing workers or their jobs."""
        with self.lock:
            for record in self.records.values():
                if record.get("closed") or record.get("container_removed"):
                    continue
                try:
                    self.collect_exit(record)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    record["cleanup_error"] = type(error).__name__
                    self.save_record(record)

    def read_workspace_files(
        self, owner: str, references: list[dict], allow_collected_results: bool = False
    ) -> tuple[dict, list]:
        """Read selected research files from the caller; jobs may also use collected results."""
        with self.lock:
            record = self.owned(owner, owner)
            if record.get("closed"):
                raise PermissionError("Closed candidate cannot supply workspace attachments")
            root = candidate_workspace(self.root, owner)
            selected, manifest, total = {}, [], 0
            if not 1 <= len(references) <= 16:
                raise ValueError("Select 1..16 workspace research files")
            for reference in references:
                name, expected = reference["path"], reference["sha256"]
                path = PurePosixPath(name)
                if (
                    name != path.as_posix()
                    or "\\" in name
                    or path.is_absolute()
                    or len(path.parts) < 2
                    or not (
                        path.parts[0] == "research"
                        or (
                            allow_collected_results
                            and path.parts[:2] == ("orchestration", "results")
                        )
                    )
                    or any(part in {"..", "."} or part.startswith(".") for part in path.parts)
                    or name in selected
                ):
                    raise ValueError("Workspace attachments need unique canonical research paths")
                directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                leaf = None
                try:
                    for part in path.parts[:-1]:
                        child = os.open(
                            part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                        )
                        os.close(directory)
                        directory = child
                    leaf = os.open(
                        path.parts[-1],
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=directory,
                    )
                    info = os.fstat(leaf)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                        raise ValueError(
                            "Workspace attachment must be a regular file of at most 1 MiB"
                        )
                    with os.fdopen(leaf, "rb") as stream:
                        leaf = None
                        raw = stream.read(1024 * 1024 + 1)
                    actual = hashlib.sha256(raw).hexdigest()
                    if len(raw) > 1024 * 1024 or actual != expected:
                        raise ValueError("Workspace attachment size or SHA256 changed")
                    total += len(raw)
                    if total > 8 * 1024 * 1024:
                        raise ValueError("Workspace attachments exceed 8 MiB")
                    selected[name] = raw.decode("utf-8")
                    manifest.append({"path": name, "sha256": actual, "bytes": len(raw)})
                except (OSError, UnicodeError) as error:
                    raise ValueError(
                        "Workspace attachment unavailable, unsafe or not UTF8"
                    ) from error
                finally:
                    if leaf is not None:
                        os.close(leaf)
                    os.close(directory)
            return selected, manifest

    def create_initial(
        self, owner: str, command: list[str], token: str, version_id: str, inputs: Path
    ) -> dict:
        if self.published_source is None:
            return self.create_version(owner, command, token, version_id, inputs=inputs)
        from a3_research_agent.host.sessions.versions import CandidateVersions

        self.versions.owned(owner, version_id)
        files = CandidateVersions.read_snapshot(self.versions.root, version_id)
        workspace = research_workspace(self.root.parent, owner)
        workspace.mkdir(parents=True, exist_ok=False)
        return self.create(
            owner, COMMAND, files, token, workspace=workspace, version_id=version_id, inputs=inputs
        )

    def create_version(
        self,
        owner: str,
        command: list[str],
        token: str,
        version_id: str,
        *,
        inputs: Path | None = None,
    ) -> dict:
        if self.published_source is not None:
            raise ValueError("Published Harness runs have one research workspace")
        workspace = self.versions.checkout(owner, version_id, owner)
        try:
            return self.create(
                owner,
                command,
                {},
                token,
                workspace=workspace,
                version_id=version_id,
                inputs=inputs,
            )
        except Exception:
            self.versions.release(owner)
            raise

    def check_capacity(self, exclude: str | None = None) -> None:
        with self.lock:
            active = sum(
                self.state(record)["Status"] in ACTIVE_STATES
                for session, record in self.records.items()
                if not record.get("closed") and session != exclude
            )
            if active >= MAX_ACTIVE_CANDIDATES:
                raise CandidateCapacityError("Candidate concurrency resources are busy")

    def load_records(self) -> None:
        for path in self.root.glob("*/session.json"):
            record = json.loads(path.read_text())
            if record["owner"] != path.parent.name:
                raise ValueError("Candidate record identity mismatch")
            self.records[path.parent.name] = record

    def suspend(self) -> None:
        errors = []
        with self.lock:
            for session, record in self.records.items():
                if record.get("closed") or record.get("container_removed"):
                    continue
                try:
                    state = self.state(record)["Status"]
                    record["resume_status"] = state
                    if state == "running":
                        self.docker("pause", record["container_id"])
                    (self.root / session / "session.json").write_text(json.dumps(record, indent=2))
                except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
                    errors.append(session)
        if errors:
            raise RuntimeError("Candidate suspension pending: " + ", ".join(errors))

    def resume_sessions(self, credentials: dict[str, str], main_owner: str) -> None:
        from a3_research_agent.host.runs.context import require_tool_results
        from a3_research_agent.host.sessions.packages import load_harness_package

        tokens = {owner: token for token, owner in credentials.items()}
        with self.lock:
            actions = []
            # Validate every required restart before allowing any paused worker to execute.
            for session, record in list(self.records.items()):
                if record.get("self_stopped"):
                    continue
                if record.get("closed") and session != main_owner:
                    continue
                if record.get("self_stop_requested"):
                    raise RuntimeError("Unconfirmed self-stop; inspect the session before recovery")
                try:
                    status = self.collect_exit(record)["Status"]
                except CandidateContainerMissingError:
                    status = "missing"
                if record.get("cleanup_error") and status in {"exited", "dead"}:
                    raise RuntimeError("Resume requires the pending exit archive to finish first")
                if status not in ACTIVE_STATES | {"exited", "dead", "missing"}:
                    raise RuntimeError("Unexpected container state; explicit recovery required")
                command = None
                if status in {"exited", "dead", "missing"} and session == main_owner:
                    workspace = candidate_workspace(self.root, session)
                    if self.published_source is None:
                        command, files = load_harness_package(workspace, "resume.json")
                    else:
                        command, files = pinned_resume(workspace)
                    if "checkpoint.json" in files:
                        checkpoint = json.loads(files["checkpoint.json"])
                        messages = (
                            checkpoint
                            if isinstance(checkpoint, list)
                            else (
                                checkpoint.get("messages") if isinstance(checkpoint, dict) else None
                            )
                        )
                        if self.published_source is not None and (
                            not isinstance(checkpoint, list)
                            or not checkpoint
                            or not all(isinstance(item, dict) for item in checkpoint)
                        ):
                            raise ValueError(
                                "Published Harness requires a nonempty message checkpoint"
                            )
                        if isinstance(messages, list) and all(
                            isinstance(item, dict) for item in messages
                        ):
                            require_tool_results(messages)
                elif status == "missing" and record.get("resume_status") in ACTIVE_STATES:
                    raise RuntimeError(
                        "An active worker container is missing; explicit recovery required"
                    )
                actions.append((session, record, status, command))
            for session, record, status, command in actions:
                if command is not None:
                    if record.get("container_removed"):
                        archived = self.root / session / "console.log"
                        if archived.exists():
                            self.write_durable(
                                archived.with_name(f"console-resume-{time.time_ns()}.log"),
                                archived.read_text(),
                            )
                    self.create(
                        record["owner"],
                        command,
                        {},
                        tokens[record["owner"]],
                        workspace=candidate_workspace(self.root, session),
                        version_id=record.get("version_id"),
                        inputs=Path(record.get("inputs", str(self.inputs))),
                        reuse=True,
                    )
                elif status == "paused":
                    self.docker("unpause", record["container_id"])
                elif status == "created":
                    self.docker("start", record["container_id"])

    def create(
        self,
        owner: str,
        command: list[str],
        files: dict[str, str],
        token: str,
        *,
        workspace: Path | None = None,
        version_id: str | None = None,
        inputs: Path | None = None,
        reuse: bool = False,
    ) -> dict:
        with self.lock:
            self.check_capacity(exclude=owner if reuse else None)
            session_id = owner
            directory = self.root / session_id
            directory.mkdir(parents=True, exist_ok=reuse)
            if workspace is None:
                workspace = directory / "workspace"
                workspace.mkdir()
            else:
                # Host-only link preserves the historical artifact lookup path.
                link = directory / "workspace"
                if not reuse:
                    link.symlink_to(workspace, target_is_directory=True)
                elif link.resolve() != workspace.resolve():
                    raise ValueError("Resume must retain the original workspace")
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
                f"type=bind,src={inputs or self.inputs},dst=/run/input,readonly",
                "--mount",
                f"type=bind,src={self.public_a3},dst=/opt/a3,readonly",
                "--workdir=/workspace",
            ]
            if self.published_source is not None:
                environment.update(
                    A3_HARNESS_ROOT="/opt/research-harness",
                    PYTHONPATH="/opt/research-harness",
                    PYTHONDONTWRITEBYTECODE="1",
                )
                arguments.extend(
                    [
                        "--mount",
                        f"type=bind,src={self.published_source},dst=/opt/research-harness,readonly",
                    ]
                )
            for key, value in environment.items():
                arguments.extend(["--env", f"{key}={value}"])
            container = self.docker(*arguments, self.image, *command).strip()
            record = {
                "owner": owner,
                "session_id": session_id,
                "container_id": container,
                "command": command,
                "closed": False,
                "version_id": version_id,
                "inputs": str(inputs or self.inputs),
            }
            self.records[session_id] = record
            (directory / "session.json").write_text(json.dumps(record, indent=2))
            try:
                self.docker("start", container)
            except Exception:
                self.cancel(owner, session_id)
                raise
            return {"session_id": session_id, "status": "created", "version_id": version_id}

    def status(self, owner: str, session_id: str) -> dict:
        with self.lock:
            record = self.owned(owner, session_id)
            if record["closed"]:
                return {"session_id": session_id, "status": "closed"}
            state = self.collect_exit(record)
            if record.get("terminal_state"):
                lines = (
                    (self.root / session_id / "console.log").read_text().splitlines(keepends=True)
                )
                output = "".join(lines[-200:])[-65536:]
            elif state["Status"] in {"exited", "dead"}:
                output = ""
            else:
                output = self.docker("logs", "--tail=200", record["container_id"])[-65536:]
            result = {
                "session_id": session_id,
                "status": state["Status"],
                "exit_code": state["ExitCode"],
                "output": output,
            }
            if record.get("cleanup_error"):
                result["cleanup_error"] = record["cleanup_error"]
            return result

    def artifact(
        self, owner: str, session_id: str, name: str, offset: int = 0, limit: int | None = None
    ) -> dict:
        self.owned(owner, session_id)
        path = workspace_path(self.root / session_id / "workspace", name)
        if not path.is_file():
            raise ValueError("Artifact must be a workspace file")
        paged = limit is not None or offset != 0
        read_limit = (
            limit
            if limit is not None
            else (DEFAULT_ARTIFACT_PAGE_BYTES if paged else MAX_ARTIFACT_BYTES)
        )
        if offset < 0 or not 1 <= read_limit <= MAX_ARTIFACT_BYTES:
            raise ValueError("Invalid artifact page bounds")
        size = path.stat().st_size
        if not paged and size > MAX_ARTIFACT_BYTES:
            raise ArtifactTooLargeError(size)
        if offset > size:
            raise ValueError("Artifact offset exceeds file size")
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(min(read_limit, size - offset))
        result = {
            "path": name,
            "encoding": "base64",
            "data": base64.b64encode(data).decode(),
        }
        if paged:
            result.update(
                offset=offset,
                next_offset=offset + len(data),
                size_bytes=size,
                eof=offset + len(data) >= size,
            )
        return result

    def stop(self, owner: str, session_id: str) -> dict:
        """End this process and reclaim its container while preserving its evidence."""
        with self.lock:
            record = self.owned(owner, session_id)
            if not record.get("self_stopped"):
                record["self_stop_requested"] = True
                self.save_record(record)
                if not record.get("container_removed"):
                    self.docker("stop", "--timeout", "5", record["container_id"])
                if self.state(record)["Status"] not in {"exited", "dead"}:
                    raise RuntimeError("Self-stop has not been confirmed by Docker")
                record["self_stopped"] = True
                self.save_record(record)
            self.collect_exit(record)
            return {"session_id": session_id, "status": "exited"}

    def cancel(self, owner: str, session_id: str) -> dict:
        with self.lock:
            record = self.owned(owner, session_id)
            if not record["closed"]:
                if not record.get("container_removed"):
                    state = self.collect_exit(record)
                    if state["Status"] in ACTIVE_STATES - {"created"}:
                        if state["Status"] == "paused":
                            self.docker("unpause", record["container_id"])
                        self.docker("stop", "--timeout", "5", record["container_id"])
                        state = self.state(record)
                    self.archive_and_remove(record, state)
                record["closed"] = True
                self.save_record(record)
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
