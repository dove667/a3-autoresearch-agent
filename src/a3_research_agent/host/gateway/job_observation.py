"""Bounded status snapshots shared by concurrent readers, independent of session locks."""

import copy
import threading
import time
from collections import OrderedDict

TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


class JobStatusCache:
    def __init__(self, *, clock=time.monotonic, capacity=512):
        self.clock = clock
        self.capacity = capacity
        self.entries = OrderedDict()
        self.lock = threading.RLock()

    def read_many(self, keys, fetch):
        """Coalesce refreshes; active snapshots live 30s, terminal snapshots 5min."""
        with self.lock:
            missing = []
            for key in keys:
                entry = self.entries.get(key)
                if entry is None or self.clock() >= entry[0]:
                    missing.append(key)
            if missing:
                values = fetch(missing)
                if set(values) != set(missing):
                    raise ValueError("Incomplete job observation response")
                for key, value in values.items():
                    ttl = 300 if value.get("status") in TERMINAL_STATES else 30
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
        for key in ("job_id", "status", "exit_code", "error", "retry_count", "observed_at")
        if key in row
    }
