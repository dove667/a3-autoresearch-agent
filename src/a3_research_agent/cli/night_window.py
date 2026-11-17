"""Run a resumable Host during the Beijing night window."""

import os
import signal
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from a3_research_agent.host.runs.directories import create_run_directory
from a3_research_agent.host.runs.state import RunStateStore

BEIJING = ZoneInfo("Asia/Shanghai")
STARTUP_RETRY_DELAYS = (15, 30, 60, 120, 300)


def end_hour() -> int:
    """Morning end of the night window, operator-configurable for early mornings."""
    raw = os.environ.get("A3_NIGHT_WINDOW_END_HOUR", "9")
    try:
        hour = int(raw)
    except ValueError:
        raise ValueError("A3_NIGHT_WINDOW_END_HOUR must be an integer hour") from None
    if not 0 <= hour <= 20:
        raise ValueError("A3_NIGHT_WINDOW_END_HOUR must be between 0 and 20")
    return hour


def window_boundary(now: datetime, end: int = 9) -> tuple[bool, datetime]:
    now = now.astimezone(BEIJING)
    morning = now.replace(hour=end, minute=0, second=0, microsecond=0)
    evening = morning.replace(hour=21)
    if now < morning:
        return True, morning
    if now < evening:
        return False, evening
    return True, morning + timedelta(days=1)


def startup_retry_delay(failed_starts: int) -> int:
    """Back off after a Host exits before it ever wrote run state."""
    index = min(failed_starts, len(STARTUP_RETRY_DELAYS)) - 1
    return STARTUP_RETRY_DELAYS[index]


def started_host(state: RunStateStore) -> bool:
    path = getattr(state, "path", None)
    return path is not None and path.is_file()


def discard_empty_run(run: Path | None) -> None:
    if run is None:
        return
    try:
        if run.is_dir() and not any(run.iterdir()):
            run.rmdir()
    except OSError:
        pass


class FreshNightSchedule:
    """Reserve a new conversation for each night while sharing research files."""

    def __init__(self, root, command):
        self.root = root
        self.command = command

    def select(self):
        run = create_run_directory(self.root / "trajectories")
        return self.command.copy(), run, {"A3_NIGHT_WINDOW_RUN_DIR": str(run)}


def run_nightly(
    command: list[str], state: RunStateStore, *, schedule=None, window_end: int = 9
) -> int:
    stop = threading.Event()
    child_env = {**os.environ, "A3_NIGHT_WINDOW_CHILD": "1"}

    def request_stop(signum, frame):
        stop.set()

    previous_handlers = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    failed_starts = 0
    try:
        while not stop.is_set():
            allowed, boundary = window_boundary(datetime.now(BEIJING), window_end)
            if not allowed:
                print(f"Host paused until {boundary.isoformat()}", flush=True)
                while not stop.is_set() and not window_boundary(
                    datetime.now(BEIJING), window_end
                )[0]:
                    stop.wait(1)
                failed_starts = 0
                continue

            launch_command, launch_state, launch_env = command, state, child_env
            run = None
            if schedule is not None:
                launch_command, run, extra_env = schedule.select()
                launch_state = RunStateStore(run)
                launch_env = {**child_env, **extra_env}

            interrupted = False
            with subprocess.Popen(launch_command, start_new_session=True, env=launch_env) as host:
                while host.poll() is None:
                    if stop.is_set() or not window_boundary(datetime.now(BEIJING), window_end)[0]:
                        host.send_signal(signal.SIGINT)
                        interrupted = True
                        break
                    stop.wait(1)

            if interrupted:
                if stop.is_set() or host.returncode != 0:
                    return host.returncode
                if launch_state.load().get("status") != "suspended":
                    raise RuntimeError("Host did not save a suspended checkpoint")
                failed_starts = 0
                continue

            if host.returncode == 0 or started_host(launch_state):
                return host.returncode

            # The Host never wrote run state, so it failed before research began.
            # Keep the window alive instead of losing the whole night to one
            # transient startup failure (for example a dropped network at 21:00).
            failed_starts += 1
            delay = startup_retry_delay(failed_starts)
            print(
                f"Host failed to start (exit {host.returncode}, attempt {failed_starts}); "
                f"retrying in {delay}s",
                flush=True,
            )
            discard_empty_run(run)
            if stop.wait(delay):
                return host.returncode
        return 0
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
