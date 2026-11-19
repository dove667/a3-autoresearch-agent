"""Jobs Gateway behavior on the shared runtime instance."""

import base64
import json
import time
import uuid
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Annotated, Literal

from fastapi import Header, HTTPException, Query

from a3_research_agent.execution.controller.files import MAX_FILE_BYTES
from a3_research_agent.host.telemetry.diagnostics import CURRENT_REQUEST

from .evaluation import EvaluationComparison, evaluation_diagnostics
from .job_observation import TERMINAL_STATES, ControllerObservationUnavailable, compact_status
from .schemas import (
    ExportGrantRequest,
    ExportRequest,
    JobObservationRequest,
    JobRequest,
    JobWaitRequest,
)


class JobRoutes:
    def research_scopes(self, actor):
        """Mutations retain the submitting session identity."""
        return [actor]

    def research_read_scopes(self, actor):
        with self.session_lock:
            scopes = self.research_scopes(actor)
            if actor in self.root_owners or actor in self.session_records:
                scopes.extend(
                    sorted((self.root_owners | self.session_records.keys()) - set(scopes))
                )
            return scopes

    def project_read_scopes(self, actor):
        scopes = self.research_read_scopes(actor)
        return list(dict.fromkeys([*scopes, *self.project_scope_runs]))

    def known_job_scope(self, actor, job_id, *, shared_read=False):
        with self.session_lock:
            scopes = (
                self.research_read_scopes(actor) if shared_read else self.research_scopes(actor)
            )
            for scope in scopes:
                if job_id in self.jobs.get(scope, set()):
                    return scope
            if not shared_read or not self.project_scope_runs:
                return None
            return self.project_job_scopes.get(job_id)

    def job_owner(self, actor, job_id, *, shared_read=False):
        return self.known_job_scope(actor, job_id, shared_read=shared_read) or actor

    def job_access(self, actor, scope):
        provenance = {"owner_scope": scope, "access": "owned" if scope == actor else "read_only"}
        if run := self.project_scope_runs.get(scope):
            provenance["owner_run"] = run
        return provenance

    def accessible_export(self, actor, export_id):
        """Authorize a sealed input export for the requesting research identity."""
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
            request = body.model_dump(
                exclude_none=True, exclude={"workspace_files", "evaluation", "name"}
            )
            if body.retry_on_gpu_contention:
                request.pop("retry_on_gpu_contention")
            source_files = []
            if body.workspace_files:
                self.own_session(authorization)
                if self.session_runtime is None:
                    raise HTTPException(503, "Native research execution is not configured")
                attached, source_files = self.deployment_call(
                    self.session_runtime.read_workspace_files,
                    experiment,
                    [x.model_dump() for x in body.workspace_files],
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
                            **self.job_access(experiment, experiment),
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
                event = CURRENT_REQUEST.get()
                if event is not None:
                    event["admission"] = {
                        key: result[key]
                        for key in ("status", "gpu_memory_mb", "gpu_capacity_mb")
                        if key in result
                    }
            if result.get("job_id"):
                self.jobs[experiment].discard(job_id)
                self.jobs.setdefault(experiment, set()).add(result["job_id"])
                self.save_checkpoint()
                receipt = {
                    "job_id": result["job_id"],
                    "experiment_id": experiment,
                    "name": body.name or "",
                    "command": request.get("command", []),
                    "gpu": bool(request.get("gpu", False)),
                    "source_files": source_files,
                    "input_exports": request.get("input_exports", []),
                    "submitted_at": time.time(),
                }
                with (
                    self.log_lock,
                    self.usage_log.with_name("job-inputs.jsonl").open("a") as stream,
                ):
                    stream.write(json.dumps(receipt) + "\n")
                if source_files:
                    result = result | {"source_files": source_files}
                result = result | self.job_access(experiment, experiment)
            return result

    def list_jobs(
        self,
        authorization: Annotated[str | None, Header()] = None,
        scope: str | None = None,
        after: str = "",
        limit: int = 200,
    ):
        actor = self.owner(authorization)
        scope = actor if not scope or scope == "self" else scope
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
        provenance = self.job_access(actor, scope)
        observed_at = time.time()
        return result | {
            "jobs": [job | provenance | {"observed_at": observed_at} for job in result["jobs"]],
            "scope": scope,
            "status_source": "controller_metadata",
            **provenance,
        }

    def remote_assets(self, body: dict, authorization: Annotated[str | None, Header()] = None):
        self.owner(authorization)
        return self.remote("remote-assets", body)

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
        if actor != self.main_owner:
            raise HTTPException(403, "Only the main session can share exports")
        record = self.session_records.get(session_id)
        if not record or record["closed"]:
            raise HTTPException(403, "Export recipient must be an active session in this run")

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

        def fetch(_):
            value = self.remote("status", {"experiment_id": scope, "job_id": job_id})
            return {key: value | {"observed_at": time.time()}}

        result = self.job_status_cache.read_many([key], fetch)[key]
        if scope == actor and result.get("job_id") == job_id:
            with self.session_lock:
                identifiers = self.jobs.setdefault(actor, set())
                if job_id not in identifiers:
                    identifiers.add(job_id)
                    self.save_checkpoint()
        if result.get("managed"):
            for name in ("gpu_uuid", "gpu_shared", "gpu_memory_mb", "gpu_allocation"):
                result.pop(name, None)
        return result | self.job_access(actor, scope)

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
                    "jobs-status",
                    {
                        "experiment_id": scope,
                        "job_ids": identifiers,
                        "allow_missing": True,
                    },
                )
                rows = response.get("jobs", [])
                unresolved = response.get("unresolved_jobs", [])
                if (
                    not isinstance(rows, list)
                    or not all(isinstance(row, dict) for row in rows)
                    or not all(isinstance(row.get("job_id"), str) for row in rows)
                    or not isinstance(unresolved, list)
                    or not all(isinstance(identifier, str) for identifier in unresolved)
                    or len(rows) + len(unresolved) != len(identifiers)
                    or {row["job_id"] for row in rows}.union(unresolved) != set(identifiers)
                ):
                    raise HTTPException(502, "Controller returned incomplete job observations")
                observed_at = time.time()
                for row in rows:
                    try:
                        values[("summary", scope, row["job_id"])] = compact_status(
                            row | {"observed_at": observed_at}
                        )
                    except ValueError:
                        raise HTTPException(502, "Controller omitted job status") from None
                for job_id in unresolved:
                    values[("summary", scope, job_id)] = {
                        "job_id": job_id,
                        "status": "observation_error",
                        "error": "job_metadata_unavailable",
                        "observed_at": observed_at,
                    }
            return values

        values = self.job_status_cache.read_many(keys, fetch)
        return [
            row | self.job_access(actor, key[1])
            if row.get("error") != "job_metadata_unavailable"
            else row
            for key, row in values.items()
        ]

    def reconcile_jobs(
        self, body: JobObservationRequest, authorization: Annotated[str | None, Header()] = None
    ):
        """Read watcher metadata for jobs already indexed to this project."""
        actor = self.owner(authorization)
        known, unresolved = [], []
        for job_id in dict.fromkeys(body.job_ids):
            if self.known_job_scope(actor, job_id, shared_read=True) is None:
                unresolved.append(job_id)
            else:
                known.append(job_id)
        try:
            rows = self.observe_jobs(actor, known)
        except ControllerObservationUnavailable as error:
            return {"jobs": [], "unresolved_jobs": unresolved, "observation": error.detail}
        return {"jobs": rows, "unresolved_jobs": unresolved, "status_source": "controller_metadata"}

    def log_job_observation_error(self, status_code):
        with (
            self.log_lock,
            self.usage_log.with_name("controller-incidents.jsonl").open("a") as stream,
        ):
            stream.write(
                json.dumps(
                    {"time": time.time(), "operation": "jobs-status", "status_code": status_code}
                )
                + "\n"
            )

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

    def list_artifacts(
        self,
        job_id: str,
        authorization: Annotated[str | None, Header()] = None,
        area: Literal["workspace", "outputs"] = "outputs",
        directory: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
    ):
        """List an authorized directory; the file read route keeps its existing contract."""
        actor = self.owner(authorization)
        payload = {
            "experiment_id": self.job_owner(actor, job_id, shared_read=True),
            "job_id": job_id,
            "area": area,
        }
        if directory is not None:
            payload["directory"] = directory
        try:
            result = self.remote("artifacts-list", payload)
        except HTTPException as error:
            if error.status_code == 400 and error.detail == "FileNotFoundError":
                raise HTTPException(
                    400,
                    {
                        "code": "artifact_not_found",
                        "area": area,
                        "hint": "This job has no outputs area yet; check job status before reading.",
                    },
                ) from None
            raise
        if (
            result.get("area") != area
            or result.get("directory") != directory
            or not isinstance(result.get("entries"), list)
        ):
            raise HTTPException(
                502,
                "Controller did not return an artifact listing; verify matching deployment versions",
            )
        return result

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
        retry_at = 0
        observation = None
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
                except ControllerObservationUnavailable as error:
                    observation = error.detail
                    retry_at = time.monotonic() + observation["retry_after_seconds"]
                else:
                    observation = None
                    if any(row["status"] in TERMINAL_STATES for row in rows):
                        reason = "terminal"
                        break
                    if any(row["status"] == "observation_error" for row in rows):
                        reason = "observation_error"
                        break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "observation_error" if observation else "timeout"
                break
            time.sleep(min(1, remaining))
            signals = self.wait_signals(authorization)
        return {
            "reason": reason,
            "jobs": rows,
            "status_source": "controller_metadata",
            **({"observation": observation} if observation else {}),
            **({"signals": signals} if reason == "notification" else {}),
        }

    def cancel(self, job_id: str, authorization: Annotated[str | None, Header()] = None):
        actor = self.owner(authorization)
        scope = self.job_owner(actor, job_id, shared_read=True)
        if scope != actor:
            raise HTTPException(
                403,
                {
                    "code": "job_read_only",
                    "job_id": job_id,
                    **self.job_access(actor, scope),
                    "hint": "Only the submitting session can cancel this job.",
                },
            )
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
        offset: Annotated[str | None, Query(pattern=r"^[0-9]{1,20}$")] = None,
        limit: Annotated[str | None, Query(pattern=r"^[0-9]{1,4}$")] = None,
        max_bytes: Annotated[str | None, Query(pattern=r"^[0-9]{1,20}$")] = None,
    ):
        actor = self.owner(authorization)
        if (offset is None) != (limit is None):
            raise HTTPException(422, "Artifact paging requires both offset and limit")
        if limit is not None and not 1 <= int(limit) <= 8000:
            raise HTTPException(422, "Artifact page limit must be in 1..8000 characters")
        if max_bytes is not None and not 1 <= int(max_bytes) <= MAX_FILE_BYTES:
            raise HTTPException(422, "Artifact byte ceiling must narrow the configured limit")
        payload = {
            "experiment_id": self.job_owner(actor, job_id, shared_read=True),
            "job_id": job_id,
            "path": path,
        }
        if area != "workspace":
            payload["area"] = area
        if offset is not None:
            payload.update(offset=int(offset), limit=int(limit))
        if max_bytes is not None:
            payload["max_bytes"] = int(max_bytes)
        try:
            result = self.remote("artifact", payload)
            if area == "outputs" and result.get("area") != "outputs":
                raise HTTPException(
                    502,
                    "Controller did not confirm outputs area; verify matching deployment versions",
                )
            if offset is not None and result.get("encoding") not in {"utf-8", "binary"}:
                raise HTTPException(
                    502, "Controller does not support artifact pages; update its deployment"
                )
            return result
        except HTTPException as error:
            if error.status_code == 400 and error.detail == "IsADirectoryError":
                raise HTTPException(400, {"code": "artifact_is_directory"}) from None
            if error.status_code == 400 and error.detail == "FileNotFoundError":
                raise HTTPException(
                    400,
                    {
                        "code": "artifact_not_found",
                        "area": area,
                        "hint": "Not found in the selected area. workspace is this job's input and source directory; outputs is this owner's shared result directory within the recorded run. Use a relative path and select area explicitly. No fallback or cross-owner access occurs; a missing file is not a reason to rerun the job.",
                    },
                ) from None
            raise
