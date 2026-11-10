"""Jobs Gateway behavior on the shared runtime instance."""

import base64
import json
import re
import time
import uuid
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Annotated, Literal

from fastapi import Header, HTTPException

from .evaluation import EvaluationComparison, evaluation_diagnostics
from .job_observation import TERMINAL_STATES, compact_status
from .schemas import (
    ExportGrantRequest,
    ExportRequest,
    JobRequest,
    JobWaitRequest,
)


class JobRoutes:
    def research_scopes(self, actor):
        """Only the adopted main inherits the recorded, retained predecessor line."""
        scopes = [actor]
        if self.evolution and actor == self.evolution.main_owner:
            for transition in reversed(self.evolution.transitions):
                if transition["to"] == scopes[-1] and transition["from"] not in scopes:
                    scopes.append(transition["from"])
        return scopes

    def research_read_scopes(self, actor):
        with self.session_lock:
            scopes = self.research_scopes(actor)
            if actor in self.root_owners or actor in self.children:
                scopes.extend(sorted((self.root_owners | self.children.keys()) - set(scopes)))
            return scopes

    def project_read_scopes(self, actor):
        scopes = self.research_read_scopes(actor)
        return list(dict.fromkeys([*scopes, *self.project_scope_runs]))

    def job_owner(self, actor, job_id, *, shared_read=False):
        with self.session_lock:
            scopes = (
                self.research_read_scopes(actor) if shared_read else self.research_scopes(actor)
            )
            for scope in scopes:
                if job_id in self.jobs.get(scope, set()):
                    return scope
            if not shared_read or not self.project_scope_runs:
                return actor
            scope = self.project_job_scopes.get(job_id)
        if scope:
            return scope
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            return actor
        # Some old Host checkpoints missed jobs accepted by the Controller.
        for scope in self.project_scope_runs:
            try:
                self.remote("jobs-status", {"experiment_id": scope, "job_ids": [job_id]})
            except HTTPException as error:
                if error.status_code != 403:
                    raise
                continue
            with self.session_lock:
                self.project_job_scopes[job_id] = scope
            return scope
        return actor

    def accessible_export(self, actor, export_id):
        """Prepare read-only mounts from research identities in this project."""
        for scope in self.project_read_scopes(actor):
            try:
                record = self.remote(
                    "export-status", {"experiment_id": scope, "export_id": export_id}
                )
            except HTTPException as error:
                if error.status_code != 403:
                    raise
                continue
            if scope != actor and record.get("status") == "ready":
                self.remote(
                    "export-grant",
                    {"experiment_id": scope, "export_id": export_id, "recipient": actor},
                )
            return scope, record
        raise HTTPException(403, "Export is not accessible")

    def validate_exports(self, actor, identifiers):
        """Validate retained/local selections with the same bounded controller requests."""
        for offset in range(0, len(identifiers), 64):
            batch = identifiers[offset : offset + 64]
            try:
                self.remote("exports-check", {"experiment_id": actor, "export_ids": batch})
            except HTTPException as error:
                if error.status_code != 403 or len(self.project_read_scopes(actor)) == 1:
                    raise
                for identifier in batch:
                    self.accessible_export(actor, identifier)
                self.remote("exports-check", {"experiment_id": actor, "export_ids": batch})

    def submit(self, body: JobRequest, authorization: Annotated[str | None, Header()] = None):
        with self.session_lock:
            self.accepting_resources()
            experiment = self.owner(authorization)
            if self.evolution:
                if experiment == self.evolution.main_owner and self.evolution.pending():
                    raise HTTPException(
                        409, "Predecessor waits for the single successor trial before new research"
                    )
                retired = any(item["from"] == experiment for item in self.evolution.transitions)
                if retired:
                    raise HTTPException(403, "Retired main cannot submit new research")
            request = body.model_dump(exclude_none=True, exclude={"workspace_files", "evaluation"})
            if body.retry_on_gpu_contention:
                request.pop("retry_on_gpu_contention")
            source_files = []
            if body.workspace_files:
                self.own_session(authorization)
                if self.sandbox is None:
                    raise HTTPException(503, "Candidate execution is not configured")
                attached, source_files = self.deployment_call(
                    self.sandbox.read_workspace_files,
                    experiment,
                    [x.model_dump() for x in body.workspace_files],
                    True,
                )
                files = dict(body.files)
                for reference, receipt in zip(body.workspace_files, source_files, strict=True):
                    destination = reference.destination or reference.path
                    path = PurePosixPath(destination)
                    if (
                        destination != path.as_posix()
                        or "\\" in destination
                        or path.is_absolute()
                        or not path.parts
                        or any(part in {"..", "."} or part.startswith(".") for part in path.parts)
                        or destination in files
                    ):
                        raise HTTPException(422, "Select unique canonical job file destinations")
                    files[destination] = attached[reference.path]
                    receipt["destination"] = destination
                names = set(files)
                if any(
                    parent.as_posix() in names
                    for name in names
                    for parent in PurePosixPath(name).parents
                ):
                    raise HTTPException(422, "Job file paths conflict with a directory")
                if (
                    len(files) > 128
                    or sum(len(x.encode("utf-8")) for x in files.values()) > 20 * 1024 * 1024
                ):
                    raise HTTPException(422, "Job input exceeds 128 files or 20 MiB")
                request["files"] = files
            if not body.input_exports:
                request.pop("input_exports")
            elif len(self.project_read_scopes(experiment)) > 1:
                self.validate_exports(experiment, body.input_exports)
            # Persist identity before sending bytes: an SSH timeout does not undo admission.
            job_id = uuid.uuid4().hex
            self.jobs.setdefault(experiment, set()).add(job_id)
            if body.evaluation is not None:
                self.job_evaluations[job_id] = body.evaluation.model_dump()
            self.save_checkpoint()
            try:
                result = self.remote(
                    "submit", request | {"experiment_id": experiment, "job_id": job_id}
                )
            except HTTPException as error:
                if error.status_code == 502:
                    raise HTTPException(
                        502,
                        {
                            "code": "submission_unknown",
                            "job_id": job_id,
                            "status_path": f"/jobs/{job_id}",
                            "hint": "Submission may still complete. Query this ID or GET /jobs; do not submit the experiment again.",
                        },
                    ) from None
                self.jobs[experiment].discard(job_id)
                self.job_evaluations.pop(job_id, None)
                self.save_checkpoint()
                raise
            if not result.get("job_id"):
                self.jobs[experiment].discard(job_id)
                self.save_checkpoint()
            if result.get("job_id"):
                self.jobs[experiment].discard(job_id)
                self.jobs.setdefault(experiment, set()).add(result["job_id"])
                self.save_checkpoint()
                if source_files:
                    receipt = {
                        "job_id": result["job_id"],
                        "experiment_id": experiment,
                        "source_files": source_files,
                    }
                    with (
                        self.log_lock,
                        self.usage_log.with_name("job-inputs.jsonl").open("a") as stream,
                    ):
                        stream.write(json.dumps(receipt) + "\n")
                    result = result | {"source_files": source_files}
            return result

    def list_jobs(
        self,
        authorization: Annotated[str | None, Header()] = None,
        scope: str | None = None,
        after: str = "",
        limit: int = 200,
    ):
        actor = self.owner(authorization)
        scope = scope or actor
        if scope not in self.project_read_scopes(actor):
            raise HTTPException(403, "Research scope is not accessible")
        historical_run = self.project_scope_runs.get(scope)
        request = {"experiment_id": scope, "after": after, "limit": limit}
        if historical_run:
            request["run_id"] = historical_run
        result = self.remote("jobs-list", request)
        with self.session_lock:
            identifiers = [job["job_id"] for job in result["jobs"]]
            if historical_run:
                self.project_job_scopes.update(dict.fromkeys(identifiers, scope))
            else:
                self.jobs.setdefault(scope, set()).update(identifiers)
                self.save_checkpoint()
        if self.project_scope_runs:
            result["historical_scopes"] = self.project_scope_runs
        return result | {"scope": scope, "status_source": "controller_metadata"}

    def gpu_snapshot(self, authorization: Annotated[str | None, Header()] = None):
        self.owner(authorization)
        key = "gpu-snapshot"
        return self.gpu_snapshot_cache.read_many([key], lambda _: {key: self.remote(key, {})})[key]

    def list_exports(self, authorization: Annotated[str | None, Header()] = None):
        actor = self.owner(authorization)
        exports = {}
        for scope in reversed(self.project_read_scopes(actor)):
            result = self.remote("exports-list", {"experiment_id": scope})
            exports.update({entry["export_id"]: entry for entry in result.get("exports", [])})
        return {"exports": list(exports.values())}

    def publish_export(
        self,
        job_id: str,
        body: ExportRequest,
        authorization: Annotated[str | None, Header()] = None,
    ):
        self.accepting_resources()
        actor = self.owner(authorization)
        return self.remote(
            "export-create",
            {
                **body.model_dump(),
                "experiment_id": self.job_owner(actor, job_id, shared_read=True),
                "job_id": job_id,
            },
        )

    def export_status(self, export_id: str, authorization: Annotated[str | None, Header()] = None):
        return self.accessible_export(self.owner(authorization), export_id)[1]

    def export_manifest(
        self, export_id: str, authorization: Annotated[str | None, Header()] = None
    ):
        scope, _ = self.accessible_export(self.owner(authorization), export_id)
        return self.remote("export-manifest", {"experiment_id": scope, "export_id": export_id})

    def require_export_recipient(self, actor, session_id):
        if self.evolution is None:
            raise HTTPException(403, "Only the current main Agent can share exports")
        # A trial can return its bounded validation assets to the predecessor.
        if (
            self.evolution.trials.get(actor, {}).get("status") == "testing"
            and session_id == self.evolution.main_owner
        ):
            return
        self.deployment_call(self.evolution.require_main, actor)
        record = self.children.get(session_id)
        if not record or record["parent"] != actor or record["closed"]:
            raise HTTPException(403, "Export recipient must be the active linear successor")

    def grant_export(
        self,
        export_id: str,
        body: ExportGrantRequest,
        authorization: Annotated[str | None, Header()] = None,
    ):
        with self.session_lock:
            self.accepting_resources()
            actor = self.owner(authorization)
            self.require_export_recipient(actor, body.session_id)
            self.accessible_export(actor, export_id)
            return self.remote(
                "export-grant",
                {"experiment_id": actor, "export_id": export_id, "recipient": body.session_id},
            )

    def revoke_export(
        self, export_id: str, session_id: str, authorization: Annotated[str | None, Header()] = None
    ):
        with self.session_lock:
            actor = self.owner(authorization)
            if self.evolution is None:
                raise HTTPException(403, "Only the current main Agent revokes worker access")
            self.deployment_call(self.evolution.require_main, actor)
            self.require_export_recipient(actor, session_id)
            _, record = self.accessible_export(actor, export_id)
            if record.get("owner") in self.project_scope_runs:
                raise HTTPException(403, "Historical exports can only be reused read-only")
            return self.remote(
                "export-revoke",
                {"experiment_id": actor, "export_id": export_id, "recipient": session_id},
            )

    def delete_export(self, export_id: str, authorization: Annotated[str | None, Header()] = None):
        actor = self.owner(authorization)
        return self.remote("export-delete", {"experiment_id": actor, "export_id": export_id})

    def status(self, job_id: str, authorization: Annotated[str | None, Header()] = None):
        actor = self.owner(authorization)
        scope = self.job_owner(actor, job_id, shared_read=True)
        key = ("detail", scope, job_id)
        result = self.job_status_cache.read_many(
            [key],
            lambda keys: {key: self.remote("status", {"experiment_id": scope, "job_id": job_id})},
        )[key]
        if result.get("managed"):
            for name in ("gpu_uuid", "gpu_shared", "gpu_memory_mb", "gpu_allocation"):
                result.pop(name, None)
        return result

    def observe_jobs(self, actor, job_ids):
        keys = [
            ("summary", self.job_owner(actor, job_id, shared_read=True), job_id)
            for job_id in dict.fromkeys(job_ids)
        ]

        def fetch(missing):
            groups = defaultdict(list)
            for _, scope, job_id in missing:
                groups[scope].append(job_id)
            values = {}
            for scope, identifiers in groups.items():
                response = self.remote(
                    "jobs-status", {"experiment_id": scope, "job_ids": identifiers}
                )
                rows = response.get("jobs", [])
                if (
                    not isinstance(rows, list)
                    or not all(isinstance(row, dict) for row in rows)
                    or len(rows) != len(identifiers)
                    or {row.get("job_id") for row in rows} != set(identifiers)
                ):
                    raise HTTPException(502, "Controller returned incomplete job observations")
                for row in rows:
                    try:
                        values[("summary", scope, row["job_id"])] = compact_status(row)
                    except ValueError:
                        raise HTTPException(502, "Controller omitted job status") from None
            return values

        values = self.job_status_cache.read_many(keys, fetch)
        return [values[key] for key in keys]

    def build_evaluation_diagnostics(self, actor, job_id, comparison):
        scopes = {
            identifier: self.job_owner(actor, identifier, shared_read=True)
            for identifier in (job_id, comparison.baseline_job_id)
        }

        def read(identifier, path, max_bytes):
            result = self.remote(
                "artifact",
                {
                    "experiment_id": scopes[identifier],
                    "job_id": identifier,
                    "path": path,
                    "area": "outputs",
                    "max_bytes": max_bytes,
                },
            )
            if result.get("area") != "outputs" or result.get("encoding") != "base64":
                raise ValueError("Controller did not confirm the artifact area")
            raw = base64.b64decode(result["data"], validate=True)
            if len(raw) > max_bytes:
                raise ValueError("Evaluation artifact exceeds its byte ceiling")
            return raw

        return evaluation_diagnostics(read, job_id, comparison)

    def replay_evaluation(
        self,
        job_id: str,
        body: EvaluationComparison,
        authorization: Annotated[str | None, Header()] = None,
    ):
        actor = self.owner(authorization)
        if any(
            row["status"] != "completed"
            for row in self.observe_jobs(actor, [job_id, body.baseline_job_id])
        ):
            raise HTTPException(409, "Both evaluation jobs must be complete")
        try:
            return self.build_evaluation_diagnostics(actor, job_id, body)
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, "Invalid evaluation manifest or report schema") from None

    def attach_evaluation_diagnostics(self, actor, rows):
        """Prepare terminal evidence outside the long-poll request's time budget."""
        enriched = []
        for row in rows:
            job_id = row["job_id"]
            comparison = self.job_evaluations.get(job_id)
            if comparison and row["status"] == "completed":
                key = (actor, job_id)
                with self.session_lock:
                    receipt = self.evaluation_receipts.get(key)
                    if receipt is None:
                        receipt = self.evaluation_workers.submit(
                            self.build_evaluation_diagnostics,
                            actor,
                            job_id,
                            EvaluationComparison.model_validate(comparison),
                        )
                        self.evaluation_receipts[key] = receipt
                if not receipt.done():
                    continue
                try:
                    evidence = receipt.result()
                except (HTTPException, KeyError, TypeError, ValueError):
                    evidence = {
                        "status": "evidence_unavailable",
                        "retry_path": f"/jobs/{job_id}/evaluation",
                        "comparison": comparison,
                    }
                row = row | {"evaluation": evidence}
            enriched.append(row)
        return enriched

    def wait_jobs(
        self, body: JobWaitRequest, authorization: Annotated[str | None, Header()] = None
    ):
        """Hold a request without a model call; running jobs do not wake the Agent."""
        self.accepting_resources()
        actor = self.owner(authorization)
        deadline = time.monotonic() + body.seconds
        signals = self.wait_signals(authorization)
        cursor = body.cursor or signals["cursor"]
        rows = []
        retry_delay = 5
        retry_at = 0
        while True:
            self.accepting_resources()
            if signals["cursor"] != cursor or (body.cursor is None and signals["pending"]):
                reason = "notification"
                break
            now = time.monotonic()
            if now >= retry_at:
                try:
                    rows = self.attach_evaluation_diagnostics(
                        actor, self.observe_jobs(actor, body.job_ids)
                    )
                except HTTPException as error:
                    if error.status_code < 500:
                        raise
                    with (
                        self.log_lock,
                        self.usage_log.with_name("controller-incidents.jsonl").open("a") as stream,
                    ):
                        stream.write(
                            json.dumps(
                                {
                                    "time": time.time(),
                                    "operation": "jobs-status",
                                    "status_code": error.status_code,
                                }
                            )
                            + "\n"
                        )
                    retry_at = time.monotonic() + retry_delay
                    retry_delay = min(30, retry_delay * 2)
                else:
                    retry_delay = 5
                    if any(row["status"] in TERMINAL_STATES for row in rows):
                        reason = "terminal"
                        break
                    if any(row["status"] == "observation_error" for row in rows):
                        reason = "observation_error"
                        break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "timeout"
                break
            time.sleep(min(1, remaining))
            signals = self.wait_signals(authorization)
        return {
            "reason": reason,
            "jobs": rows,
            "status_source": "controller_metadata",
            **({"signals": signals} if reason == "notification" else {}),
        }

    def cancel(self, job_id: str, authorization: Annotated[str | None, Header()] = None):
        actor = self.owner(authorization)
        scope = self.job_owner(actor, job_id)
        try:
            return self.remote("cancel", {"experiment_id": scope, "job_id": job_id})
        finally:
            self.job_status_cache.invalidate(scope, job_id)

    def artifact(
        self,
        job_id: str,
        path: str,
        authorization: Annotated[str | None, Header()] = None,
        area: Literal["workspace", "outputs"] = "workspace",
    ):
        actor = self.owner(authorization)
        payload = {
            "experiment_id": self.job_owner(actor, job_id, shared_read=True),
            "job_id": job_id,
            "path": path,
        }
        if area != "workspace":
            payload["area"] = area
        try:
            result = self.remote("artifact", payload)
            if area == "outputs" and result.get("area") != "outputs":
                raise HTTPException(
                    502,
                    "Controller did not confirm outputs area; verify matching deployment versions",
                )
            return result
        except HTTPException as error:
            if error.status_code == 400 and error.detail == "FileNotFoundError":
                raise HTTPException(
                    400,
                    {
                        "code": "artifact_not_found",
                        "area": area,
                        "hint": "Not found in the selected area. workspace is this job's /workspace; outputs is this owner's shared /outputs within the recorded run. Use a relative path and select area explicitly. No fallback or cross-owner access occurs; a missing file is not a reason to rerun the job.",
                    },
                ) from None
            raise
