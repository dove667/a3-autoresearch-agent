"""Bounded status snapshots shared by concurrent readers, independent of session locks."""

import copy
import threading
import time
from collections import OrderedDict
from math import ceil

from fastapi import HTTPException

TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


class ControllerObservationUnavailable(HTTPException):
    def __init__(self, retry_after_seconds):
        super().__init__(
            503,
            {
                "code": "controller_observation_unavailable",
                "status": "controller_unavailable",
                "retry_after_seconds": retry_after_seconds,
            },
        )


class JobStatusCache:
    def __init__(self, *, clock=time.monotonic, capacity=512, on_transport_error=None):
        self.clock = clock
        self.capacity = capacity
        self.on_transport_error = on_transport_error
        self.entries = OrderedDict()
        self.lock = threading.RLock()
        self.retry_at = 0
        self.retry_delay = 5

    def fetch(self, keys, fetch):
        if self.clock() < self.retry_at:
            raise ControllerObservationUnavailable(ceil(self.retry_at - self.clock()))
        try:
            values = fetch(keys)
        except HTTPException as error:
            if self.on_transport_error is None or error.status_code < 500:
                raise
            delay = self.retry_delay
            self.retry_at = self.clock() + delay
            self.retry_delay = min(30, delay * 2)
            self.on_transport_error(error.status_code)
            raise ControllerObservationUnavailable(delay) from None
        self.retry_at, self.retry_delay = 0, 5
        return values

    def read_many(self, keys, fetch):
        """Coalesce refreshes; active snapshots live 30s, terminal snapshots 5min."""
        with self.lock:
            missing = []
            for key in keys:
                entry = self.entries.get(key)
                if entry is None or self.clock() >= entry[0]:
                    missing.append(key)
            if missing:
                values = self.fetch(missing, fetch)
                if set(values) != set(missing):
                    raise ValueError("Incomplete job observation response")
                for key, value in values.items():
                    status = value.get("status")
                    ttl = 300 if status in TERMINAL_STATES else 30
                    self.entries[key] = (self.clock() + ttl, copy.deepcopy(value))
            result = {key: copy.deepcopy(self.entries[key][1]) for key in keys}
            for key in keys:
                self.entries.move_to_end(key)
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)
            return result

    def invalidate(self, owner, job_id):
        with self.lock:
            for kind in ("detail", "summary"):
                self.entries.pop((kind, owner, job_id), None)


def compact_status(row):
    if not isinstance(row.get("status"), str) or not row["status"]:
        raise ValueError("Controller omitted job status")
    return {
        key: row[key]
        for key in (
            "job_id",
            "status",
            "exit_code",
            "error",
            "retry_count",
            "observed_at",
            "benchmark_receipt",
        )
        if key in row
    }
