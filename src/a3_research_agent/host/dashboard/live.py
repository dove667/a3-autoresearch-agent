"""Read job status from the running Host instead of the Agent's last snapshot.

A job shown as "queued" after it has been running for half an hour is worse than no
status at all: the Agent records job state only when it polls, and between polls the
overview describes the past. The Host answers job status through its own Gateway, so
the dashboard asks it, with a short cache so a five-second browser refresh does not turn
into a stream of remote calls.

Two callers exist. Inside the Host the Gateway client is already at hand. A dashboard
started on its own has no client, so it reads the run's loopback endpoint and token from
`.host/state.json`; the token is used for this local read and never leaves the process.
"""

import json
import threading
import time
from pathlib import Path

TTL = 10.0


class JobStatusFeed:
    """Poll the Gateway for a run's job states, at most once per interval."""

    def __init__(self, client=None, root=None, ttl=TTL):
        self.client = client
        self.root = Path(root) if root is not None else None
        self.ttl = ttl
        self.lock = threading.Lock()
        self.read_at = {}
        self.cache = {}

    def statuses(self, run_id=None):
        """Return job_id -> {status, exit_code}; the last known answer on failure."""
        key = run_id or ""
        now = time.monotonic()
        with self.lock:
            if now - self.read_at.get(key, 0.0) < self.ttl:
                return dict(self.cache.get(key, {}))
            self.read_at[key] = now
        try:
            fresh = self._fetch(run_id)
        except Exception:  # noqa: BLE001 - a read-only view never raises into the page
            return dict(self.cache.get(key, {}))
        with self.lock:
            self.cache[key] = fresh
        return dict(fresh)

    def _fetch(self, run_id):
        owned = self.client is None
        client = self._run_client(run_id) if owned else self.client()
        try:
            return self._read(client)
        finally:
            if owned:
                client.close()

    def _read(self, client):
        sessions = client.get("/sessions", params={"include_status": "true"}).json()["sessions"]
        result = {}
        for session in sessions:
            if session.get("closed"):
                continue
            identifier = session.get("session_id")
            if not identifier:
                continue
            rows = client.get("/jobs", params={"scope": identifier, "limit": 200}).json()
            for job in rows.get("jobs", []):
                if isinstance(job, dict) and job.get("job_id"):
                    result[job["job_id"]] = {
                        "status": job.get("status"),
                        "exit_code": job.get("exit_code"),
                    }
        return result

    def _run_client(self, run_id):
        """Open the run's own Gateway when the dashboard runs outside it."""
        if not run_id or self.root is None:
            raise RuntimeError("no run to query")
        import httpx

        state = json.loads(
            (self.root / "trajectories" / run_id / ".host/state.json").read_text()
        )
        if state.get("status") not in {"running", "starting"}:
            raise RuntimeError("run is not live")
        token = next(iter(state["gateway"]["sessions"]))
        return httpx.Client(
            base_url=state["endpoint"],
            headers={"Authorization": "Bearer " + token},
            timeout=5,
            trust_env=False,
        )
