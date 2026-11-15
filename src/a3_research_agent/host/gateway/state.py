"""State Gateway behavior on the shared runtime instance."""

import json
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager, nullcontext

from fastapi import HTTPException

from a3_research_agent.host.sessions.workspace import (
    DEFAULT_ARTIFACT_PAGE_BYTES,
    MAX_ACTIVE_SESSIONS,
    MAX_ARTIFACT_BYTES,
    ArtifactTooLargeError,
    SessionCapacityError,
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
                    self.session_runtime.reap_exited()
                except Exception as error:  # noqa: BLE001 — retry after Host storage failures
                    logging.getLogger(__name__).error(
                        "Research process cleanup will retry (%s)", type(error).__name__
                    )

        reaper = None
        if self.session_runtime is not None and hasattr(self.session_runtime, "reap_exited"):
            reaper = threading.Thread(
                target=reap_workers, name="research-process-reaper", daemon=True
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
            self.evaluation_workers.shutdown(wait=False, cancel_futures=True)
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
                        "session_records": self.session_records,
                        "jobs": {key: sorted(value) for key, value in self.jobs.items()},
                        "job_evaluations": self.job_evaluations,
                        "main_owner": self.main_owner,
                        "fixtures": self.fixtures.records if self.fixtures is not None else {},
                    }
                )

    def result_location(self):
        with self.session_lock:
            return self.main_owner

    def session_event(self, event, session_id):
        record = self.session_records[session_id]
        entry = {
            "event": event,
            "session_id": session_id,
            "parent": record["parent"],
            "time": time.time(),
        }
        with self.log_lock, (self.debug_root / "session-events.jsonl").open("a") as stream:
            stream.write(json.dumps(entry) + "\n")
        self.save_checkpoint()

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

    def close_session(self, session_id):
        with self.session_lock:
            record = self.session_records[session_id]
            if record["closed"]:
                return {"session_id": session_id, "status": "closed"}
            self.sessions.pop(record["token"], None)
            errors = self.close_owner_resources(record["owner"])
            try:
                self.session_runtime.cancel(record["owner"], session_id)
            except Exception:  # noqa: BLE001 — retain failed cleanup for an explicit retry
                errors.append("research process " + session_id)
            if errors:
                self.save_checkpoint()
                raise RuntimeError("Cleanup pending: " + ", ".join(errors))
            record["closed"] = True
            self.session_event("closed", session_id)
            return {"session_id": session_id, "status": "closed"}

    def close_resources(self):
        self.begin_shutdown()
        errors = []
        with self.session_lock:
            for session_id in list(self.session_records):
                try:
                    self.close_session(session_id)
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

    def deployment_call(self, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
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
        except SessionCapacityError as error:
            self.diagnostics.error("session_capacity", error)
            raise HTTPException(
                409,
                {
                    "reason": "session_capacity",
                    "max_active": MAX_ACTIVE_SESSIONS,
                    "retry_after_seconds": 2,
                },
            ) from None
        except Exception as error:  # noqa: BLE001 — do not expose process or transport details
            self.diagnostics.error("deployment", error)
            raise HTTPException(
                409, "Deployment operation failed; check available resources"
            ) from None

    def managed_session(self, authorization, session_id):
        record = self.session_records.get(session_id)
        if record is None or self.owner(authorization) not in self.root_owners:
            raise HTTPException(403, "Research session is not accessible")
        return record

    def own_session(self, authorization):
        experiment = self.owner(authorization)
        record = self.session_records.get(experiment)
        if record is None or record["owner"] != experiment:
            raise HTTPException(403, "This identity has no research session")
        return record

    def readable_session(self, authorization, session_id):
        """Allow registered identities to read this run's research workspaces."""
        self.owner(authorization)
        record = self.session_records.get(session_id)
        if record is None:
            raise HTTPException(403, "Session is not registered in this run")
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
