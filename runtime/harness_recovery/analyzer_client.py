"""Low-frequency advisory scheduling; never execute or automatically accept advice."""

import copy
import json
import math
import pathlib
import threading
import time

import context_manager

POLL_SECONDS = 60
PERIOD_SECONDS = 30 * 60
COOLDOWN_SECONDS = 10 * 60
REQUEST_TIMEOUT = 2
SCHEDULE_PATH = pathlib.Path("analysis-client/schedule.json")


class Controller:
    """Persist reservations before I/O so restarts cannot erase rate limits.

    signals returns cumulative counters: decisions, progress_updates and
    repeated_static_reads (or repeated_reads for content/range-aware readers).
    It may also return waiting=True for a legitimate external-job wait. The
    callback must be local and must not call the analyzer or a model endpoint.
    """

    def __init__(self, req, signals, *, waiting=None, clock=time.time, path=SCHEDULE_PATH):
        self.req, self.signals, self.waiting = req, signals, waiting
        self.clock, self.path = clock, pathlib.Path(path)
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.disabled = False
        try:
            self.state = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {
                    "version": 1,
                    "last_poll": None,
                    "last_attempt": None,
                    "last_periodic": None,
                    "samples": [],
                    "latest": None,
                }
            )
            if self.state.get("version") != 1 or not isinstance(self.state.get("samples"), list):
                raise ValueError("Invalid analyzer schedule")
            latest = self.state.get("latest")
            if latest is not None and (
                not isinstance(latest, dict)
                or not isinstance(latest.get("pending_reports", []), list)
                or any(not isinstance(item, dict) for item in latest.get("pending_reports", []))
            ):
                raise ValueError("Invalid persisted advisory")
            for key in (
                "last_poll",
                "last_attempt",
                "last_periodic",
                "progress_at",
                "last_event_check",
            ):
                value = self.state.get(key)
                if value is not None and (
                    type(value) not in (int, float) or not math.isfinite(value) or value < 0
                ):
                    raise ValueError("Invalid persisted scheduler time")

        except (OSError, ValueError, TypeError, AttributeError):
            # Preserve damaged state; do not silently reset spent reservations.
            self.state, self.disabled = {}, True

    def _save(self):
        context_manager.atomic(self.path, json.dumps(self.state, ensure_ascii=False))

    def _request(self, method, path, data=None):
        value = self.req(method, path, data, timeout=REQUEST_TIMEOUT)
        if not isinstance(value, dict) or value.get("http_error"):
            raise ValueError("Analyzer service unavailable")
        return value

    def _is_main(self):
        own = self._request("GET", "/sessions/self")
        evolution = self._request("GET", "/evolution")
        return bool(own.get("session_id")) and own["session_id"] == evolution.get("main_owner")

    def _observe(self, now):
        values = self.signals()
        if not isinstance(values, dict):
            raise TypeError("Invalid analyzer signals")
        counters = {}
        for key in ("decisions", "progress_updates", "repeated_static_reads"):
            value = values.get(
                key, values.get("repeated_reads", 0) if key == "repeated_static_reads" else 0
            )
            if type(value) is not int or value < 0:
                raise ValueError("Invalid cumulative analyzer counter")
            counters[key] = value
        waiting = self.waiting() if self.waiting else values.get("waiting", False)
        previous = self.state.get("counters")
        reset = previous is None or any(counters[k] < previous[k] for k in counters)
        if reset or counters["progress_updates"] != previous["progress_updates"] or waiting:
            self.state["progress_at"] = now
            self.state["progress_decisions"] = counters["decisions"]
        samples = [] if reset else self.state["samples"]
        samples.append([now, counters["repeated_static_reads"]])
        # A delta with an older baseline could include reads outside the window.
        while len(samples) > 1 and samples[0][0] < now - COOLDOWN_SECONDS:
            samples.pop(0)
        self.state["samples"] = samples[-12:]
        self.state["counters"] = counters
        repeated = counters["repeated_static_reads"] - samples[0][1] >= 10
        stalled = (
            not waiting
            and now - self.state["progress_at"] >= COOLDOWN_SECONDS
            and counters["decisions"] - self.state["progress_decisions"] >= 20
        )
        return repeated, stalled

    def _check(self, requested=None):
        if not self.lock.acquire(blocking=False):
            return self.view()
        try:
            if self.disabled or self.stopped.is_set():
                return self.view()
            try:
                now = self.clock()
                last_poll = self.state.get("last_poll")
                if requested is None and last_poll is not None and now - last_poll < POLL_SECONDS:
                    return self.view()
                if requested is not None:
                    attempted = self.state.get("last_attempt")
                    if attempted is not None and now - attempted < COOLDOWN_SECONDS:
                        return self.view()
                    checked = self.state.get("last_event_check")
                    if checked is not None and now - checked < POLL_SECONDS:
                        return self.view()
                    self.state["last_event_check"] = now
                self.state["last_poll"] = now
                self._save()
                if not self._is_main():
                    return self.view()
                if self.state.get("last_periodic") is None:
                    self.state["last_periodic"] = now
                repeated, stalled = self._observe(now)
                self._save()
                latest = self._request("GET", "/analysis")
                self.state["latest"] = latest
                self._save()
                if latest.get("status") == "running":
                    return self.view()
                periodic = now - self.state["last_periodic"] >= PERIOD_SECONDS
                trigger = requested or (
                    "periodic"
                    if periodic
                    else "repetition"
                    if repeated
                    else "no_progress"
                    if stalled
                    else None
                )
                attempted = self.state.get("last_attempt")
                if trigger is None or (
                    attempted is not None and now - attempted < COOLDOWN_SECONDS
                ):
                    return self.view()
                self.state["last_attempt"] = now
                if trigger == "periodic":
                    self.state["last_periodic"] = now
                self.state["progress_at"] = now
                self.state["progress_decisions"] = self.state["counters"]["decisions"]
                self.state["samples"] = [[now, self.state["counters"]["repeated_static_reads"]]]
                self._save()  # Includes failed/uncertain POSTs in the shared cooldown.
                report = self._request("POST", "/analysis", {"trigger": trigger})
                # POST returns one report; preserve the GET backlog until refreshed.
                self.state["latest"] = {
                    **{
                        key: latest[key]
                        for key in ("pending_reports", "omitted_pending_reports", "history")
                        if key in latest
                    },
                    **report,
                }
                self._save()
            except Exception as error:  # noqa: BLE001 - advisory failures must not gate research
                self.state["last_error"] = type(error).__name__
            return self.view()
        finally:
            self.lock.release()

    def poll(self):
        return self._check()

    def trigger(self, event):
        if event != "before_handoff":
            raise ValueError("Only the before_handoff lifecycle event is supported")
        return self._check(event)

    def view(self):
        """Return advisory data and a response contract; never apply suggestions."""
        if self.disabled:
            return {
                "status": "disabled",
                "reason": "invalid_persisted_schedule",
                "advisory_only": True,
                "response_required": False,
            }
        # Reports are replaced wholesale, never mutated; reading the last published
        # reference does not wait for background identity or network checks.
        latest = copy.deepcopy(self.state.get("latest"))
        if not latest:
            return None
        latest["advisory_only"] = True
        pending = latest.get("pending_reports", [])
        latest["response_required"] = (
            bool(pending)
            or latest.get("status")
            in {
                "completed",
                "success",
            }
            and not latest.get("response")
        )
        if latest["response_required"]:
            latest["response_contract"] = {
                "method": "POST",
                "path": "/analysis/response",
                "analysis_id": pending[0]["analysis_id"] if pending else latest.get("analysis_id"),
                "decision": ["adopt", "reject", "defer"],
                "reason_required": True,
            }
        return latest

    def close(self):
        self.stopped.set()

    def _run(self):
        while not self.stopped.is_set():
            self.poll()
            self.stopped.wait(POLL_SECONDS)


def start(req, signals):
    controller = Controller(req, signals)
    threading.Thread(target=controller._run, name="advisory-scheduler", daemon=True).start()
    return controller
