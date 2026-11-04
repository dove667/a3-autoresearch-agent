"""State Gateway behavior on the shared runtime instance."""

import json
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager, nullcontext

from fastapi import HTTPException

from a3_research_agent.host.sessions.containers import (
    DEFAULT_ARTIFACT_PAGE_BYTES,
    MAX_ACTIVE_CANDIDATES,
    MAX_ARTIFACT_BYTES,
    ArtifactTooLargeError,
    CandidateCapacityError,
)
from a3_research_agent.host.telemetry.diagnostics import CURRENT_REQUEST


class GatewayState:
    @asynccontextmanager
    async def lifespan(self, app):
        # Poll independently of model calls and Agent status requests, including during H0.
        stopped = threading.Event()

        def reap_workers():
            while not stopped.wait(2):
                try:
                    self.sandbox.reap_exited()
                except Exception as error:  # noqa: BLE001 — retry after Host storage/daemon failures
                    logging.getLogger(__name__).error(
                        "Worker container cleanup will retry (%s)", type(error).__name__
                    )

        reaper = None
        if self.sandbox is not None and hasattr(self.sandbox, "reap_exited"):
            reaper = threading.Thread(
                target=reap_workers, name="worker-container-reaper", daemon=True
            )
            reaper.start()
        budget_monitor = None
        if self.resource_monitor is not None:

            def monitor_storage():
                while not stopped.is_set():
                    try:
                        self.resource_monitor()
                    except Exception as error:  # noqa: BLE001 — retry transport failures
                        logging.getLogger(__name__).error(
                            "Storage usage report will retry (%s)", type(error).__name__
                        )
                    if stopped.wait(5):
                        break

            budget_monitor = threading.Thread(
                target=monitor_storage, name="run-storage-monitor", daemon=True
            )
            budget_monitor.start()
        try:
            yield
        finally:
            stopped.set()
            if reaper is not None:
                reaper.join()
            if budget_monitor is not None:
                budget_monitor.join(timeout=1)

    def save_checkpoint(self):
        with self.session_lock:
            if self.state_store is not None:
                self.state_store.update(
                    gateway={
                        "telemetry_observations": dict(self.telemetry.observations),
                        "sessions": dict(self.sessions),
                        "root_owners": sorted(self.root_owners),
                        "children": self.children,
                        "scope_parents": self.scope_parents,
                        "jobs": {key: sorted(value) for key, value in self.jobs.items()},
                        "evolution": self.evolution.snapshot() if self.evolution else None,
                        "fixtures": self.fixtures.records if self.fixtures is not None else {},
                    }
                )

    def result_location(self):
        with self.session_lock:
            actor = self.evolution.main_owner if self.evolution else self.main_owner
            record = self.children.get(actor, {})
            return actor, record.get("version_id")

    def session_event(self, event, session_id):
        record = self.children[session_id]
        entry = {
            "event": event,
            "session_id": session_id,
            "parent": record["parent"],
            "time": time.time(),
            "version_id": record.get("version_id"),
        }
        if record.get("assembly") is not None:
            entry["assembly"] = record["assembly"]
        with self.log_lock, self.usage_log.with_name("session-events.jsonl").open("a") as stream:
            stream.write(json.dumps(entry) + "\n")
        self.save_checkpoint()

    def descendants(self, session_id):
        by_parent: dict[str, list[str]] = {}
        for candidate, record in self.children.items():
            by_parent.setdefault(record["parent"], []).append(candidate)
        branch, pending = [], [session_id]
        while pending:
            candidate = pending.pop()
            branch.append(candidate)
            pending.extend(by_parent.get(self.children[candidate]["owner"], []))
        return branch

    def manages(self, experiment, record):
        ancestor = record["parent"]
        while ancestor:
            if ancestor == experiment:
                return True
            ancestor = self.scope_parents.get(ancestor)
        return False

    def accepting_resources(self):
        if self.app.state.closing:
            raise HTTPException(503, "Deployment is shutting down")

    def begin_shutdown(self):
        with self.input_versions.lock if self.input_versions else nullcontext(), self.session_lock:
            self.app.state.closing = True

    def close_owner_resources(self, experiment):
        errors = []
        if self.fixtures is not None:
            try:
                self.fixtures.close_owner(experiment)
            except Exception:  # noqa: BLE001 — continue independent cleanup operations
                errors.append("isolation fixtures")
        for job_id in list(self.jobs.get(experiment, set())):
            try:
                self.remote("cancel", {"experiment_id": experiment, "job_id": job_id})
                self.jobs[experiment].discard(job_id)
            except Exception:  # noqa: BLE001 — keep failed jobs available for cleanup retry
                errors.append("remote job " + job_id)
        return errors

    def close_child(self, session_id):
        with self.session_lock:
            errors = []
            for candidate in reversed(self.descendants(session_id)):
                record = self.children[candidate]
                if record["closed"]:
                    continue
                self.sessions.pop(record["token"], None)
                candidate_errors = self.close_owner_resources(record["owner"])
                try:
                    self.sandbox.cancel(record["owner"], candidate)
                except Exception:  # noqa: BLE001 — retain ancestor management for retry
                    candidate_errors.append("candidate " + candidate)
                if not candidate_errors:
                    record["closed"] = True
                    self.session_event("closed", candidate)
                errors.extend(candidate_errors)
            if errors:
                raise RuntimeError("Cleanup pending: " + ", ".join(errors))
            return {"session_id": session_id, "status": "closed"}

    def close_resources(self):
        self.begin_shutdown()
        errors = []
        with self.session_lock:
            for session_id in list(self.children):
                try:
                    self.close_child(session_id)
                except RuntimeError:
                    errors.append(session_id)
            for experiment in self.root_owners:
                errors.extend(self.close_owner_resources(experiment))
        if errors:
            raise RuntimeError("Deployment cleanup pending: " + ", ".join(errors))
        self.save_checkpoint()

    def owner(self, authorization: str | None) -> str:
        token = (authorization or "").removeprefix("Bearer ")
        for expected, experiment in tuple(self.sessions.items()):
            if secrets.compare_digest(token, expected):
                request = CURRENT_REQUEST.get()
                if request is not None:
                    request["experiment_id"] = experiment
                return experiment
        raise HTTPException(401, "Invalid experiment token")

    def telemetry_scope(self, actor, descendants=False):
        def allowed(candidate):
            if candidate == actor:
                return True
            if not descendants:
                return False
            seen = set()
            while candidate in self.scope_parents and candidate not in seen:
                seen.add(candidate)
                candidate = self.scope_parents[candidate]
                if candidate == actor:
                    return True
            return False

        return allowed

    def deployment_call(self, operation, *args):
        try:
            return operation(*args)
        except PermissionError:
            raise HTTPException(403, "Resource is not accessible") from None
        except ArtifactTooLargeError as error:
            raise HTTPException(
                422,
                {
                    "reason": "artifact_too_large",
                    "size_bytes": error.size_bytes,
                    "max_bytes": MAX_ARTIFACT_BYTES,
                    "retry_query": {"offset": 0, "limit": DEFAULT_ARTIFACT_PAGE_BYTES},
                },
            ) from None
        except ValueError:
            raise HTTPException(422, "Invalid workspace file or input size") from None
        except CandidateCapacityError as error:
            self.diagnostics.error("candidate_capacity", error)
            raise HTTPException(
                409,
                {
                    "reason": "candidate_capacity",
                    "max_active": MAX_ACTIVE_CANDIDATES,
                    "retry_after_seconds": 2,
                },
            ) from None
        except Exception as error:  # noqa: BLE001 — do not expose Docker or transport details
            self.diagnostics.error("deployment", error)
            raise HTTPException(
                409, "Deployment operation failed; check available resources"
            ) from None

    def child(self, authorization, session_id):
        record = self.children.get(session_id)
        if record is None or not self.manages(self.owner(authorization), record):
            raise HTTPException(403, "Candidate session is not accessible")
        return record

    def own_session(self, authorization):
        experiment = self.owner(authorization)
        record = self.children.get(experiment)
        if record is None or record["owner"] != experiment:
            raise HTTPException(403, "This identity has no candidate session")
        return record

    def remote(self, operation: str, payload: dict) -> dict:
        try:
            result = self.remote_call(operation, payload)
        except Exception as error:  # noqa: BLE001 — never return provider or SSH credential details
            self.diagnostics.error("remote_transport", error)
            raise HTTPException(502, "Remote controller unavailable") from None
        if result.get("status") == "error":
            request = CURRENT_REQUEST.get()
            if request is not None:
                request.update(error_layer="remote_controller", error_type="ControllerError")
            raise HTTPException(
                403 if result["error"] == "PermissionError" else 400, result["error"]
            )
        if result.get("status") == "resource_busy":
            request = CURRENT_REQUEST.get()
            if request is not None:
                request["wait_reason"] = result.get("reason", "remote_capacity")
                request["retry_after_seconds"] = result.get("retry_after_seconds")
        return result

    def health(self):
        return {"status": "ready"}
