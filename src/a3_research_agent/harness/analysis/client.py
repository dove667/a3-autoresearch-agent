"""Low-frequency advisory scheduling; never execute or automatically accept advice."""

import copy
import json
import math
import pathlib
import threading
import time
import uuid

from a3_research_agent.harness.context import manager as context_manager

POLL_SECONDS = 60
PERIOD_SECONDS = 30 * 60
COOLDOWN_SECONDS = 10 * 60
MAX_RETRY_SECONDS = 60 * 60
TRIGGERS = {"periodic", "repetition", "no_progress", "before_handoff"}
REQUEST_TIMEOUT = 2
SCHEDULE_PATH = pathlib.Path("analysis-client/schedule.json")


class RequestUnavailable(ValueError):
    def __init__(self, status=None):
        super().__init__("Analyzer service unavailable")
        self.status = status


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
        self.handoff_path = self.path.with_suffix(".handoff.json")
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
            # Keep version-1 snapshots readable; no failed attempt becomes success.
            self.state.setdefault("started_at", self.state.get("last_periodic"))
            self.state.setdefault("pending", {})
            self.state.setdefault("attempt", None)
            self.state.setdefault("failures", 0)
            self.state.setdefault("retry_at", 0)
            if not isinstance(self.state["pending"], dict) or any(
                key not in TRIGGERS
                or type(value) not in (int, float)
                or not math.isfinite(value)
                or value < 0
                for key, value in self.state["pending"].items()
            ):
                raise ValueError("Invalid pending analyzer signals")
            attempt = self.state["attempt"]
            if attempt is not None and (
                not isinstance(attempt, dict) or not isinstance(attempt.get("reasons"), dict)
            ):
                raise ValueError("Invalid analyzer reservation")
            if type(self.state["failures"]) is not int or not 0 <= self.state["failures"] <= 4:
                raise ValueError("Invalid failure count")
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
                "started_at",
                "retry_at",
                "last_handoff_check",
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
        if not isinstance(value, dict):
            raise RequestUnavailable()
        if value.get("http_error"):
            status = value["http_error"]
            raise RequestUnavailable(status if type(status) is int else None)
        return value

    def _is_main(self):
        own = self._request("GET", "/sessions/self")
        evolution = self._request("GET", "/evolution")
        session_id = own.get("session_id")
        if not session_id or session_id != evolution.get("main_owner"):
            return False
        if self.state.get("main_identity") not in (None, session_id):
            # Old-main private reports are not readable by its successor. Keep queued
            # reasons, but let the successor request its own authorized snapshot.
            self.state["attempt"] = None
            self.state["latest"] = None
        self.state["main_identity"] = session_id
        return True

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

    def _enqueue(self, trigger, now, *, distinct=False):
        # Coalesce each kind; keep a signal even if its rolling window later expires.
        pending = self.state["pending"]
        attempt = self.state.get("attempt")
        previous = pending.get(trigger)
        if (
            distinct
            and attempt
            and previous is not None
            and (attempt["reasons"].get(trigger) == previous)
        ):
            pending[trigger] = max(now, previous + 0.000001)
        else:
            pending.setdefault(trigger, now)

    def _collect_handoff(self, now):
        if not self.handoff_path.exists():
            return False
        notice = json.loads(self.handoff_path.read_text())
        notice_id = notice.get("id")
        if notice_id is None:  # Fresh-task reset.
            return False
        if not isinstance(notice_id, str) or len(notice_id) != 32:
            raise ValueError("Invalid persisted handoff signal")
        if notice_id != self.state.get("handoff_seen"):
            self._enqueue("before_handoff", now, distinct=True)
            self.state["handoff_seen"] = notice_id
            return True
        return False

    def _finish_attempt(self, report, now):
        attempt = self.state.get("attempt")
        if attempt is None or report.get("status") == "running":
            return
        if report.get("status") == "completed":
            if "periodic" in attempt["reasons"]:
                self.state["last_periodic"] = now
            for trigger, observed_at in attempt["reasons"].items():
                if self.state["pending"].get(trigger) == observed_at:
                    self.state["pending"].pop(trigger, None)
            self.state["failures"] = 0
            self.state["retry_at"] = 0
        else:
            failures = min(self.state["failures"] + 1, 4)
            self.state["failures"] = failures
            delay = min(COOLDOWN_SECONDS * 2 ** (failures - 1), MAX_RETRY_SECONDS)
            provider_retry = report.get("retry_not_before", 0)
            if type(provider_retry) not in (int, float) or not math.isfinite(provider_retry):
                provider_retry = 0
            self.state["retry_at"] = max(now + delay, provider_retry)
        self.state["attempt"] = None
        self._save()

    def _check(self):
        if not self.lock.acquire(blocking=False):
            return self.view()
        try:
            if self.disabled or self.stopped.is_set():
                return self.view()
            try:
                now = self.clock()
                last_poll = self.state.get("last_poll")
                # Handoff is durably enqueued by trigger even when this lock is busy.
                has_notice = self.handoff_path.exists()
                if last_poll is not None and now - last_poll < POLL_SECONDS and not has_notice:
                    return self.view()
                # Notices can remain on disk indefinitely; throttle network independently.
                new_notice = self._collect_handoff(now)
                self._save()
                checked = self.state.get("last_event_check")
                handoff_checked = self.state.get("last_handoff_check")
                urgent = new_notice and (
                    handoff_checked is None or now - handoff_checked >= POLL_SECONDS
                )
                if checked is not None and now - checked < POLL_SECONDS and not urgent:
                    return self.view()
                if urgent:
                    self.state["last_handoff_check"] = now
                self.state["last_poll"] = now
                self.state["last_event_check"] = now
                self._save()
                if not self._is_main():
                    return self.view()
                if self.state["started_at"] is None:
                    self.state["started_at"] = now
                if self.state.get("handoff_seen") != self.state.get("handoff_host_sent"):
                    self._request(
                        "POST", "/analysis", {"trigger": "before_handoff", "enqueue_only": True}
                    )
                    self.state["handoff_host_sent"] = self.state.get("handoff_seen")
                    self._save()
                repeated, stalled = self._observe(now)
                if repeated:
                    self._enqueue("repetition", now, distinct=True)
                if stalled:
                    self._enqueue("no_progress", now, distinct=True)
                baseline = self.state.get("last_periodic")
                if (
                    now - (self.state["started_at"] if baseline is None else baseline)
                    >= PERIOD_SECONDS
                ):
                    self._enqueue("periodic", now)
                self._save()
                latest = self._request("GET", "/analysis")
                self.state["latest"] = latest
                for signal in latest.get("deferred_triggers", []):
                    if signal in TRIGGERS:
                        self._enqueue(signal, now)
                attempt = self.state.get("attempt")
                if attempt and attempt.get("analysis_id"):
                    report = latest
                    if report.get("analysis_id") != attempt["analysis_id"]:
                        try:
                            report = self._request("GET", "/analysis/" + attempt["analysis_id"])
                        except RequestUnavailable as error:
                            if error.status != 404:
                                raise
                            # Bounded Host history may legitimately evict an old
                            # reserved report. It is unknown, never a new success.
                            report = {"status": "evicted"}
                    self._finish_attempt(report, now)
                elif attempt and latest.get("status") != "running":
                    # Crash between durable reservation and saving the POST response.
                    # The Host may have accepted it; never infer success from an
                    # unrelated old report. Reconcile as uncertain with bounded delay,
                    # then let the Host fingerprint cache prevent duplicate work.
                    self._finish_attempt({"status": "interrupted"}, now)
                self._save()
                if latest.get("status") == "running" or self.state.get("attempt"):
                    return self.view()
                pending = self.state["pending"]
                attempted = self.state.get("last_attempt")
                if (
                    not pending
                    or now < self.state["retry_at"]
                    or (attempted is not None and now - attempted < COOLDOWN_SECONDS)
                ):
                    return self.view()
                # One fresh snapshot can cover all currently pending signals. Periodic
                # takes precedence so handoff/repetition bursts cannot starve it.
                trigger = next(
                    key
                    for key in ("periodic", "before_handoff", "repetition", "no_progress")
                    if key in pending
                )
                self.state["last_attempt"] = now
                self.state["attempt"] = {"reasons": dict(pending), "analysis_id": None}
                self.state["progress_at"] = now
                self.state["progress_decisions"] = self.state["counters"]["decisions"]
                self.state["samples"] = [[now, self.state["counters"]["repeated_static_reads"]]]
                self._save()  # Reserve before POST, including uncertain/failed requests.
                try:
                    report = self._request("POST", "/analysis", {"trigger": trigger})
                except Exception:
                    self._finish_attempt({"status": "failed"}, now)
                    raise
                self.state["attempt"]["analysis_id"] = report.get("analysis_id")
                self.state["latest"] = {
                    **{
                        key: latest[key]
                        for key in ("pending_reports", "omitted_pending_reports", "history")
                        if key in latest
                    },
                    **report,
                }
                self._finish_attempt(report, now)
                self._save()
            except Exception as error:  # noqa: BLE001 - advisory failures never gate research
                self.state["last_error"] = type(error).__name__
            return self.view()
        finally:
            self.lock.release()

    def poll(self):
        return self._check()

    def trigger(self, event):
        if event != "before_handoff":
            raise ValueError("Only the before_handoff lifecycle event is supported")
        if self.disabled or self.stopped.is_set():
            return self.view()
        try:
            # A separate atomic notice coalesces lifecycle bursts without waiting for
            # background network I/O. Its ID is acknowledged in the durable schedule.
            context_manager.atomic(self.handoff_path, json.dumps({"id": uuid.uuid4().hex}))
        except Exception as error:  # noqa: BLE001 - do not block the lifecycle operation
            self.state["last_error"] = type(error).__name__
            return self.view()
        return self._check()

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
