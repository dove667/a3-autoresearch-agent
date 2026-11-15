"""Run a resumable Host during the Beijing night window."""

import json
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from a3_research_agent.host.runs.state import RunStateStore

BEIJING = ZoneInfo("Asia/Shanghai")


def window_boundary(now: datetime) -> tuple[bool, datetime]:
    now = now.astimezone(BEIJING)
    morning = now.replace(hour=9, minute=0, second=0, microsecond=0)
    evening = morning.replace(hour=21)
    if now < morning:
        return True, morning
    if now < evening:
        return False, evening
    return True, morning + timedelta(days=1)


def exhausted_pinned_model_requests(run: Path, state: dict, launched_at: float) -> bool:
    """Recognize a freshly suspended run on a release with fixed model retries."""
    actor = state.get("gateway", {}).get("evolution", {}).get("main_owner")
    if state.get("status") != "suspended" or not isinstance(actor, str):
        return False
    if not actor or Path(actor).name != actor or actor in {".", ".."}:
        return False
    workspace = run / "candidates" / actor / "workspace"
    if not (workspace / "checkpoint.json").is_file():
        return False
    try:
        failure = json.loads((workspace / "loop-failure.json").read_text())
        failed_at = failure["time"]
        if (
            failure.get("phase") != "model_request"
            or failure.get("loop_error") != "RuntimeError"
            or failed_at < launched_at
        ):
            return False
        with (run / "gateway-events.jsonl").open("rb") as events:
            end = events.seek(0, os.SEEK_END)
            if end > 65536:
                events.seek(end - 65536)
                events.readline()
            else:
                events.seek(0)
            records = [json.loads(line) for line in events if line.endswith(b"\n")]
        requests = [
            record
            for record in records
            if record.get("event") == "finished"
            and record.get("route") == "/v1/responses"
            and record.get("experiment_id") == actor
            and launched_at <= record["time"] <= failed_at
        ][-4:]
        return (
            len(requests) == 4
            and failed_at - requests[-1]["time"] <= 10
            and all(
                record.get("error_layer") == "model_provider"
                and record.get("status_code") in {408, 429, 500, 502, 503, 504}
                for record in requests
            )
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def run_nightly(command: list[str], state: RunStateStore, *, schedule=None) -> int:
    stop = threading.Event()
    child_env = {**os.environ, "A3_NIGHT_WINDOW_CHILD": "1"}

    def request_stop(signum, frame):
        stop.set()

    previous_handlers = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    retry_run = None
    retry_delay = 30
    try:
        while not stop.is_set():
            allowed, boundary = window_boundary(datetime.now(BEIJING))
            if not allowed:
                retry_run = None
                retry_delay = 30
                print(f"Host paused until {boundary.isoformat()}", flush=True)
                while not stop.is_set() and not window_boundary(datetime.now(BEIJING))[0]:
                    stop.wait(1)
                continue

            launch_command, launch_state, launch_env = command, state, child_env
            if retry_run is not None:
                resume_index = command.index("--resume-from")
                launch_command = [*command[: resume_index + 1], str(retry_run)]
                launch_state = RunStateStore(retry_run)
            elif schedule is not None:
                launch_command, run, extra_env = schedule.select()
                launch_state = RunStateStore(run)
                launch_env = {**child_env, **extra_env}

            launched_at = time.time()
            with subprocess.Popen(launch_command, start_new_session=True, env=launch_env) as host:
                while host.poll() is None:
                    if stop.is_set() or not window_boundary(datetime.now(BEIJING))[0]:
                        host.send_signal(signal.SIGINT)
                        break
                    stop.wait(1)
                else:
                    if (
                        stop.is_set()
                        or host.returncode != 0
                        or schedule is None
                        or not launch_state.path.is_file()
                    ):
                        return host.returncode
                    run = launch_state.path.parent.parent
                    if not exhausted_pinned_model_requests(run, launch_state.load(), launched_at):
                        return host.returncode
                    # Register a just-started release before resuming its checkpoint.
                    schedule.record_started_release(run)
                    if time.time() - launched_at >= 600:
                        retry_delay = 30
                    print(
                        f"Model upstream unavailable; resuming {run.name} in {retry_delay}s",
                        flush=True,
                    )
                    retry_run = run
                    stop.wait(retry_delay)
                    retry_delay = min(300, retry_delay * 2)
                    continue

            if stop.is_set() or host.returncode != 0:
                return host.returncode
            if launch_state.load().get("status") != "suspended":
                raise RuntimeError("Host did not save a suspended checkpoint")
        return 0
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
