"""Run a resumable Host during the Beijing night window."""

import os
import signal
import subprocess
import threading
from datetime import datetime, timedelta
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


def run_nightly(command: list[str], state: RunStateStore, *, schedule=None) -> int:
    stop = threading.Event()
    child_env = {**os.environ, "A3_NIGHT_WINDOW_CHILD": "1"}

    def request_stop(signum, frame):
        stop.set()

    previous_handlers = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        while not stop.is_set():
            allowed, boundary = window_boundary(datetime.now(BEIJING))
            if not allowed:
                print(f"Host paused until {boundary.isoformat()}", flush=True)
                while not stop.is_set() and not window_boundary(datetime.now(BEIJING))[0]:
                    stop.wait(1)
                continue

            launch_command, launch_state, launch_env = command, state, child_env
            if schedule is not None:
                launch_command, run, extra_env = schedule.select()
                launch_state = RunStateStore(run)
                launch_env = {**child_env, **extra_env}

            with subprocess.Popen(launch_command, start_new_session=True, env=launch_env) as host:
                while host.poll() is None:
                    if stop.is_set() or not window_boundary(datetime.now(BEIJING))[0]:
                        host.send_signal(signal.SIGINT)
                        break
                    stop.wait(1)
                else:
                    return host.returncode

            if stop.is_set() or host.returncode != 0:
                return host.returncode
            if launch_state.load().get("status") != "suspended":
                raise RuntimeError("Host did not save a suspended checkpoint")
        return 0
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
