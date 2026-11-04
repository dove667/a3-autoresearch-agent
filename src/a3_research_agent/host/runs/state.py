"""Private Host checkpoints and exclusive ownership of a run directory."""

import fcntl
import json
import os
import threading
from pathlib import Path


class RunStateStore:
    def __init__(self, run: Path):
        self.directory = run / ".host"
        self.path = self.directory / "state.json"
        self.lock = threading.RLock()

    def load(self) -> dict:
        return json.loads(self.path.read_text())

    def update(self, **changes) -> None:
        with self.lock:
            state = self.load() if self.path.exists() else {}
            self.save({**state, **changes})

    def save(self, state: dict) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with open(temporary, "w", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
            json.dump(state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)


class RunLease:
    def __init__(self, run: Path):
        self.run = run
        self.stream = None

    def __enter__(self):
        directory = self.run / ".host"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.stream = (directory / "runtime.lock").open("a")
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            raise RuntimeError("A Host is already running for this run") from None
        return self

    def __exit__(self, *args):
        if self.stream is not None:
            self.stream.close()
