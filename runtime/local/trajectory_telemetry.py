"""Bounded, allowlisted operational counters; never read raw gateway logs."""

import copy
import hashlib
import json
import math
import threading
from collections import deque

KINDS = frozenset(
    {
        "model",
        "model_context",
        "research_web",
        "remote_job",
        "candidate",
        "candidate_version",
        "comparison",
        "isolation_fixture",
    }
)
ROLES = frozenset({"main", "worker", "candidate", "summary", "analyzer"})
OBSERVATIONS = frozenset(
    {
        "read_calls",
        "repeated_reads",
        "retrieved_characters",
        "delivered_reads",
        "open_items",
        "waiting_items",
        "closed_items",
    }
)
MAX_EVENTS = 256
MAX_RECENT_EVENTS = 32
CONTROL_ROUTES = frozenset({"/sessions/self", "/evolution"})


def bounded_number(value, maximum=1_000_000_000):
    if type(value) not in (int, float) or value < 0:
        return None
    if type(value) is float and not math.isfinite(value):
        return None
    return min(value, maximum)


class TrajectoryTelemetry:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = deque(maxlen=MAX_EVENTS)
        self.observations = {}

    def record(self, event):
        # Diagnostics control endpoints, unknown routes and unauthenticated calls never enter.
        if event.get("event") != "finished" or event.get("request_kind") not in KINDS:
            return
        actor = event.get("experiment_id")
        if not actor or event.get("route") in {"unmatched", *CONTROL_ROUTES}:
            return
        item = {"kind": event["request_kind"], "failed": int(event.get("status_code", 500) >= 400)}
        wall = bounded_number(event.get("wall_seconds"), 86400)
        if wall is not None:
            item["wall_seconds"] = round(wall, 6)
        model = event.get("model_observation")
        if isinstance(model, dict) and model.get("role") in ROLES:
            item["model"] = {"role": model["role"]}
            for key in ("input_tokens", "output_tokens", "total_tokens", "wall_seconds"):
                value = bounded_number(model.get(key))
                if value is not None:
                    item["model"][key] = value
        with self.lock:
            self.events.append((actor, item))

    def observe(self, actor, values):
        with self.lock:
            # Bound owner cardinality as well as event cardinality.
            if actor not in self.observations and len(self.observations) >= MAX_EVENTS:
                del self.observations[next(iter(self.observations))]
            self.observations[actor] = dict(values)

    def snapshot(self, allowed):
        with self.lock:
            events = [copy.deepcopy(item) for actor, item in self.events if allowed(actor)]
            observations = [item for actor, item in self.observations.items() if allowed(actor)]
        counters = {}
        models = {}
        for item in events:
            total = counters.setdefault(
                item["kind"], {"calls": 0, "failures": 0, "wall_seconds": 0}
            )
            total["calls"] += 1
            total["failures"] += item["failed"]
            total["wall_seconds"] = round(total["wall_seconds"] + item.get("wall_seconds", 0), 6)
            if model := item.get("model"):
                total = models.setdefault(
                    model["role"],
                    {
                        "calls": 0,
                        "wall_seconds": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "usage_observed_calls": 0,
                        "cost": None,
                    },
                )
                total["calls"] += 1
                total["wall_seconds"] = round(
                    total["wall_seconds"] + model.get("wall_seconds", 0), 6
                )
                total["usage_observed_calls"] += int(
                    any(key in model for key in ("input_tokens", "output_tokens", "total_tokens"))
                )
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    total[key] += model.get(key, 0)
        result = {
            "schema_version": 1,
            "window_capacity": MAX_EVENTS,
            "observed_events": len(events),
            "recent_events": events[-MAX_RECENT_EVENTS:],
            "recent_event_limit": MAX_RECENT_EVENTS,
            "requests": counters,
            "models": models,
            "local_observations": {
                key: sum(item.get(key, 0) for item in observations) for key in sorted(OBSERVATIONS)
            },
            "observation_owners": len(observations),
        }
        return result

    @staticmethod
    def fingerprint(snapshot):
        return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
