"""Run only the maintainer-published Harness as a credential-scoped local process."""

import ast
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from a3_research_agent.harness import state_paths
from a3_research_agent.harness.plan import store as plan_store
from a3_research_agent.host.published_harness import (
    COMMAND,
    conversation_root,
    project_for_run,
    research_workspace,
)
from a3_research_agent.host.runs.directories import debug_directory
from a3_research_agent.path_safety import workspace_path

from .artifact_pages import read_session_page
from .workspace import (
    ACTIVE_STATES,
    MAX_ACTIVE_SESSIONS,
    SessionCapacityError,
    exit_archived,
    read_artifact,
    read_workspace_files,
    require_native_record,
    session_workspace,
    write_durable,
    write_workspace_files,
)


def process_identity(pid):
    """Observe a saved process without granting permission to signal it."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("Invalid local process identity")
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True,
        text=True,
        env={"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"},
        check=False,
        timeout=5,
    )
    if result.returncode == 1:
        return None
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("Cannot observe the saved local process")
    return {"pid": pid, "started": result.stdout.strip()}


def process_state(status, exit_code=None):
    return {
        "status": status,
        "exit_code": exit_code,
    }


def benchmark_root(name, fallback):
    """Let an explicit benchmark root share read-only knowledge across projects.

    Production leaves the variables unset, so the per-project layout remains the
    only default. Benchmark runs set an absolute directory only when they need the
    same memory/skills source across otherwise independent project roots.
    """
    value = os.environ.get(name)
    if not value:
        return fallback
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError(f"{name} must name an absolute real directory")
    return path.resolve()


class ProcessRuntime:
    """Execute published code with explicit local process ownership and exit evidence."""

    def __init__(
        self,
        *,
        root,
        inputs,
        public_a3,
        endpoint,
        published_source,
        python=None,
        knowledge_environment=None,
    ):
        address = urlsplit(endpoint)
        if (
            address.scheme != "http"
            or address.hostname not in {"127.0.0.1", "::1", "localhost"}
            or address.username
            or address.password
            or address.path not in {"", "/"}
            or address.query
            or address.fragment
        ):
            raise ValueError("Native Harness gateway must use an HTTP loopback address")
        if published_source is None:
            raise ValueError("Native execution requires a fixed published Harness source")
        self.published_source = self.require_native_source(published_source)
        self.root, self.inputs = Path(root), Path(inputs)
        if self.root.name != "sessions":
            raise ValueError("Session metadata must use the current run/sessions layout")
        self.state_root = conversation_root(self.root.parent)
        self.plan_file = plan_store.project_plan_file(project_for_run(self.root.parent))
        self.plan_snapshots = workspace_path(self.root.parent, "plan-snapshots")
        self.public_a3, self.endpoint = Path(public_a3), endpoint
        self.logs = debug_directory(self.root.parent)
        self.python = str(Path(python or sys.executable).absolute())
        self.knowledge_environment = dict(knowledge_environment or {})
        self.records = {}
        self.lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self._processes = {}

    def console_path(self, owner):
        return self.logs / owner / "console.log"

    def owned(self, owner, session_id):
        record = self.records.get(session_id)
        if record is None or record["owner"] != owner:
            raise PermissionError("Session is not accessible")
        return record

    def save_record(self, record):
        require_native_record(record)
        write_durable(self.root / record["owner"] / "session.json", json.dumps(record, indent=2))

    def load_records(self):
        for path in self.root.glob("*/session.json"):
            record = json.loads(path.read_text())
            require_native_record(record)
            if (
                record.get("owner") != path.parent.name
                or record.get("session_id") != path.parent.name
            ):
                raise ValueError("Session record identity mismatch")
            self.records[path.parent.name] = record

    def check_capacity(self, exclude=None):
        with self.lock:
            active = sum(
                self.state(record)["status"] in ACTIVE_STATES
                for session, record in self.records.items()
                if not record.get("closed") and session != exclude
            )
            if active >= MAX_ACTIVE_SESSIONS:
                raise SessionCapacityError("The run already has an active research session")

    def read_workspace_files(self, owner, references):
        with self.lock:
            record = self.owned(owner, owner)
            if record.get("closed"):
                raise PermissionError("Closed session cannot supply source attachments")
            return read_workspace_files(session_workspace(self.root, owner), references)

    def artifact(self, owner, session_id, name, offset=0, limit=None):
        self.owned(owner, session_id)
        if name.startswith("conversation/"):
            return read_artifact(self.state_root, name.removeprefix("conversation/"), offset, limit)
        return read_artifact(session_workspace(self.root, session_id), name, offset, limit)

    def artifact_text(self, owner, session_id, name, offset=0, limit=4000):
        self.owned(owner, session_id)
        if name.startswith("conversation/"):
            return read_session_page(
                self.state_root, name.removeprefix("conversation/"), offset, limit
            )
        return read_session_page(session_workspace(self.root, session_id), name, offset, limit)

    @staticmethod
    def require_native_source(source):
        source = Path(source).resolve()
        if not all(
            (source / "a3_research_agent/harness" / name).is_file()
            for name in ("agent.py", "paths.py", "state_paths.py")
        ):
            raise ValueError(
                "Published Harness source is incomplete; expected agent.py, paths.py and state_paths.py"
            )
        layout = ast.parse((source / "a3_research_agent/harness/state_paths.py").read_text())
        if not any(
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "LAYOUT_VERSION"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is int
            and node.value.value == state_paths.LAYOUT_VERSION
            for node in layout.body
        ):
            raise ValueError(
                "Published Harness state layout requires a current maintenance release"
            )
        return source

    @staticmethod
    def require_command(command):
        if command != COMMAND:
            raise PermissionError(
                "Native execution accepts only the fixed published Harness command"
            )

    def _handle(self, record):
        entry = self._processes.get(record["owner"])
        return entry[1] if entry and entry[0] == record.get("launch_id") else None

    def state(self, record):
        require_native_record(record)
        handle = self._handle(record)
        if handle is not None:
            code = handle.poll()
            return process_state("running") if code is None else process_state("exited", code)
        if record.get("terminal_state"):
            return record["terminal_state"]
        if record.get("launch_pending") or not record.get("process"):
            raise RuntimeError("Local launch outcome is uncertain; inspect it before recovery")
        saved = record["process"]
        current = process_identity(saved["pid"])
        if current == saved:
            return process_state("running")
        # An orphan that disappeared has an unknown exit code, never inferred success.
        return process_state("exited")

    def archive_exit(self, record, state):
        if state["status"] not in {"exited", "dead"}:
            raise RuntimeError("Cannot archive a local process that may still be executing")
        session_workspace(self.root, record["owner"]).resolve(strict=True)
        console = self.console_path(record["owner"])
        if not console.exists():
            write_durable(console, "")
        with console.open("rb") as stream:
            os.fsync(stream.fileno())
        plan_store.snapshot(self.plan_file, self.plan_snapshots)
        record["terminal_state"] = state
        record["exit_saved_at"] = time.time()
        record["exit_archived"] = True
        record.pop("cleanup_error", None)
        self.save_record(record)

    def collect_exit(self, record):
        state = self.state(record)
        if state["status"] in {"exited", "dead"} and not exit_archived(record):
            self.archive_exit(record, state)
        return state

    def reap_exited(self):
        with self.lock:
            for record in self.records.values():
                if record.get("closed"):
                    continue
                try:
                    self.collect_exit(record)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    record["cleanup_error"] = type(error).__name__
                    self.save_record(record)

    def environment(self, workspace, inputs, token):
        directories = {name: self.state_root / name for name in (".home", ".tmp", ".cache")}
        for directory in directories.values():
            directory.mkdir(parents=True, exist_ok=True)
        memory = benchmark_root(
            "A3_BENCHMARK_MEMORY_ROOT",
            workspace_path(project_for_run(self.root.parent), "memory").resolve(),
        )
        memory.mkdir(exist_ok=True)
        skills = benchmark_root(
            "A3_BENCHMARK_SKILLS_ROOT",
            workspace_path(project_for_run(self.root.parent), "skills").resolve(),
        )
        skills_history = benchmark_root(
            "A3_BENCHMARK_SKILLS_HISTORY_ROOT",
            workspace_path(
                project_for_run(self.root.parent), ".runtime/skill-revisions"
            ).resolve(),
        )
        return {
            "PATH": str(Path(self.python).parent) + os.pathsep + os.defpath,
            "HOME": str(directories[".home"]),
            "TMPDIR": str(directories[".tmp"]),
            "XDG_CACHE_HOME": str(directories[".cache"]),
            "LANG": "en_US.UTF-8",
            "PYTHONPATH": str(self.published_source.resolve()),
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "A3_GATEWAY_URL": self.endpoint.rstrip("/"),
            "A3_GATEWAY_TOKEN": token,
            "A3_WORKSPACE": str(workspace),
            "A3_STATE_ROOT": str(self.state_root),
            "A3_PLAN_FILE": str(self.plan_file),
            "A3_PLAN_SNAPSHOTS_ROOT": str(self.plan_snapshots),
            "A3_SKILLS_ROOT": str(skills),
            "A3_SKILLS_HISTORY_ROOT": str(skills_history),
            "A3_MEMORY_ROOT": str(memory),
            **self.knowledge_environment,
            "A3_INPUT_ROOT": str(inputs),
            "A3_REFERENCE_ROOT": str(self.public_a3.resolve()),
            "A3_HARNESS_ROOT": str(self.published_source.resolve()),
            "A3_PROJECT_ROOT": str(self.public_a3.resolve()),
            "A3_RUN_DIR": str(workspace / "results"),
            "A3_DEPLOYMENT_FILE": str(inputs / "deployment.json"),
        }

    def create(
        self,
        owner,
        command,
        files,
        token,
        *,
        workspace=None,
        inputs=None,
        reuse=False,
    ):
        self.require_command(command)
        if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", owner):
            raise ValueError("Invalid local session identity")
        source = self.published_source.resolve()
        if not (source / "a3_research_agent/harness/agent.py").is_file():
            raise ValueError("Published Harness source is missing its fixed entry point")
        with self.lock:
            self.check_capacity(exclude=owner if reuse else None)
            previous = self.records.get(owner)
            if reuse and previous is not None:
                state = self.collect_exit(previous)
                if state["status"] in ACTIVE_STATES:
                    raise RuntimeError("Stop the previous local process before restarting it")
            directory = workspace_path(self.root, owner)
            link = directory / "workspace"
            expected = research_workspace(self.root.parent, owner)
            if workspace is None:
                workspace = expected
                workspace.mkdir(parents=True, exist_ok=True)
            else:
                workspace = Path(workspace).resolve(strict=True)
                if workspace != expected.resolve(strict=True):
                    raise ValueError("Session requires its current Host-managed research workspace")
            if not reuse and self.state_root.exists() and any(self.state_root.iterdir()):
                raise ValueError(
                    "Existing conversation state must be resumed before starting a process"
                )
            state_names = {state_paths.CHECKPOINT, state_paths.OUTCOME}
            state_files = {name: value for name, value in files.items() if name in state_names}
            research_files = {}
            for name, content in files.items():
                if name in state_names:
                    continue
                target = workspace_path(workspace, name)
                if target.exists():
                    if not target.is_file() or target.read_bytes() != content.encode("utf-8"):
                        raise ValueError("Seed research file conflicts with the shared workspace")
                else:
                    research_files[name] = content
            directory.mkdir(parents=True, exist_ok=reuse)
            if not reuse:
                link.symlink_to(workspace, target_is_directory=True)
            elif link.resolve(strict=True) != workspace.resolve(strict=True):
                raise ValueError("Native resume must retain the original workspace")
            workspace = workspace.resolve()
            if source.is_relative_to(workspace) or workspace.is_relative_to(source):
                raise ValueError("Published source must be separate from writable research files")
            if reuse and state_files:
                raise ValueError("Resume must preserve the existing conversation checkpoint")
            self.state_root.mkdir(parents=True, exist_ok=True)
            write_workspace_files(self.state_root, state_files)
            write_workspace_files(workspace, research_files)
            selected_inputs = Path(inputs or self.inputs).resolve(strict=True)
            environment = self.environment(workspace, selected_inputs, token)
            plan_store.snapshot(self.plan_file, self.plan_snapshots)
            launches = list(previous.get("previous_launches", [])) if previous else []
            if previous:
                launches.append(
                    {
                        key: previous.get(key)
                        for key in (
                            "backend",
                            "process",
                            "terminal_state",
                            "exit_saved_at",
                            "published_source",
                        )
                    }
                )
            record = {
                "version": 1,
                "owner": owner,
                "session_id": owner,
                "backend": "process",
                "exit_archived": False,
                "command": COMMAND.copy(),
                "closed": False,
                "inputs": str(selected_inputs),
                "published_source": str(source),
                "launch_id": uuid.uuid4().hex,
                "launch_pending": True,
                "previous_launches": launches,
            }
            self.records[owner] = record
            self.save_record(record)
            handle = None
            try:
                console = self.console_path(owner)
                console.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with console.open("ab") as output:
                    handle = subprocess.Popen(
                        [self.python, *COMMAND[1:]],
                        cwd=workspace,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                self._processes[owner] = (record["launch_id"], handle)
                record["process"] = process_identity(handle.pid)
                record["launch_pending"] = False
                if record["process"] is None and handle.poll() is None:
                    raise RuntimeError("Cannot record the native process identity")
                if handle.poll() is not None:
                    record["terminal_state"] = process_state("exited", handle.returncode)
                self.save_record(record)
            except Exception:
                if handle is not None and handle.poll() is None:
                    self._stop_process(record)
                else:
                    record.update(launch_pending=False, terminal_state=process_state("exited"))
                    self.save_record(record)
                raise
            return {
                "session_id": owner,
                "status": "created",
                "backend": "process",
            }

    def status(self, owner, session_id):
        with self.lock:
            record = self.owned(owner, session_id)
            if record.get("closed"):
                return {"session_id": session_id, "status": "closed"}
            state = self.collect_exit(record)
            console = self.console_path(session_id)
            output = ""
            if console.is_file():
                with console.open("rb") as stream:
                    stream.seek(max(0, console.stat().st_size - 65536))
                    output = "".join(
                        stream.read()
                        .decode("utf-8", errors="replace")
                        .splitlines(keepends=True)[-200:]
                    )
            result = {
                "session_id": session_id,
                "status": state["status"],
                "exit_code": state["exit_code"],
                "output": output,
                "backend": "process",
            }
            if record.get("cleanup_error"):
                result["cleanup_error"] = record["cleanup_error"]
            return result

    def _stop_process(self, record):
        state = self.collect_exit(record)
        if state["status"] not in ACTIVE_STATES:
            return state
        handle = self._handle(record)
        if handle is None:
            raise RuntimeError(
                "Live native process belongs to a previous Host; inspect it before recovery"
            )
        for signum, timeout in ((signal.SIGINT, 8), (signal.SIGTERM, 3), (signal.SIGKILL, 3)):
            if handle.poll() is not None:
                break
            handle.send_signal(signum)
            try:
                handle.wait(timeout=timeout)
                break
            except subprocess.TimeoutExpired:
                record["shutdown_escalated"] = True
        if handle.poll() is None:
            raise RuntimeError("Native process has not confirmed its exit")
        return self.collect_exit(record)

    def suspend(self):
        with self.lock:
            for record in self.records.values():
                if record.get("closed") or record.get("self_stopped"):
                    continue
                record["resume_status"] = self.state(record)["status"]
                self._stop_process(record)
                record["suspended"] = True
                self.save_record(record)

    def prepare_release_restart(self, owner):
        with self.lock:
            record = self.owned(owner, owner)
            self._stop_process(record)
            record["resume_status"] = "exited"
            self.save_record(record)

    def preflight_resume(self, main_owner):
        """Reject live or uncertain saved execution before reopening its Gateway identity."""
        with self.lock:
            self.owned(main_owner, main_owner)
            for record in self.records.values():
                require_native_record(record)
                if record.get("launch_pending"):
                    raise RuntimeError(
                        "Local launch outcome is uncertain; inspect it before recovery"
                    )
                handle = self._handle(record)
                if handle is not None:
                    if handle.poll() is None:
                        raise RuntimeError(
                            "Live native process belongs to a previous Host; inspect it before recovery"
                        )
                    continue
                saved = record.get("process")
                if saved:
                    if process_identity(saved["pid"]) == saved:
                        raise RuntimeError(
                            "Live native process belongs to a previous Host; inspect it before recovery"
                        )
                elif record.get("terminal_state", {}).get("status") not in {"exited", "dead"}:
                    raise RuntimeError(
                        "Local launch outcome is uncertain; inspect it before recovery"
                    )

    def resume_sessions(self, credentials, main_owner):
        from a3_research_agent.host.runs.checkpoints import require_tool_results

        tokens = {owner: token for token, owner in credentials.items()}
        with self.lock:
            record = self.owned(main_owner, main_owner)
            if record.get("self_stopped"):
                return
            if record.get("self_stop_requested"):
                raise RuntimeError("Unconfirmed self-stop; inspect the session before recovery")
            status = self.collect_exit(record)["status"]
            if status in ACTIVE_STATES:
                if self._handle(record) is None:
                    raise RuntimeError(
                        "Live native process belongs to a previous Host; inspect it before recovery"
                    )
                return
            workspace = session_workspace(self.root, main_owner)
            checkpoint = json.loads(
                state_paths.path(state_paths.CHECKPOINT, self.state_root).read_text()
            )
            if not isinstance(checkpoint, list) or not all(
                isinstance(item, dict) for item in checkpoint
            ):
                raise ValueError("Published Harness requires a message-list checkpoint")
            require_tool_results(checkpoint)
            self.create(
                main_owner,
                COMMAND,
                {},
                tokens[main_owner],
                workspace=workspace,
                inputs=Path(record.get("inputs", str(self.inputs))),
                reuse=True,
            )

    def stop(self, owner, session_id):
        with self.lock:
            record = self.owned(owner, session_id)
            if not record.get("self_stopped"):
                record["self_stop_requested"] = True
                self.save_record(record)
                self._stop_process(record)
                record["self_stopped"] = True
                self.save_record(record)
            return {"session_id": session_id, "status": "exited"}

    def cancel(self, owner, session_id):
        with self.lock:
            record = self.owned(owner, session_id)
            if not record.get("closed"):
                self._stop_process(record)
                record["closed"] = True
                self.save_record(record)
            return {"session_id": session_id, "status": "closed"}

    def close(self):
        errors = []
        with self.lock:
            for session, record in self.records.items():
                try:
                    self.cancel(record["owner"], session)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    errors.append(session)
        if errors:
            raise RuntimeError("Native process cleanup pending: " + ", ".join(errors))
