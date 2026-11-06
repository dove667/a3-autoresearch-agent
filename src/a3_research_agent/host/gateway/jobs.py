"""Jobs Gateway behavior on the shared runtime instance."""

import json
import uuid
from pathlib import PurePosixPath
from typing import Annotated, Literal

from fastapi import Header, HTTPException

from .schemas import (
    ExportGrantRequest,
    ExportRequest,
    JobRequest,
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

    def job_owner(self, actor, job_id, *, shared_read=False):
        with self.session_lock:
            scopes = (
                self.research_read_scopes(actor) if shared_read else self.research_scopes(actor)
            )
            for scope in scopes:
                if job_id in self.jobs.get(scope, set()):
                    return scope
        return actor

    def accessible_export(self, actor, export_id):
        """Materialize same-run read access before mounting shared research assets."""
        for scope in self.research_read_scopes(actor):
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
                if error.status_code != 403 or len(self.research_read_scopes(actor)) == 1:
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
            request = body.model_dump(exclude_none=True, exclude={"workspace_files"})
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
            elif len(self.research_read_scopes(experiment)) > 1:
                self.validate_exports(experiment, body.input_exports)
            # Persist identity before sending bytes: an SSH timeout does not undo admission.
            job_id = uuid.uuid4().hex
            self.jobs.setdefault(experiment, set()).add(job_id)
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
        if scope not in self.research_read_scopes(actor):
            raise HTTPException(403, "Research scope is not accessible")
        with self.session_lock:
            result = self.remote(
                "jobs-list",
                {
                    "experiment_id": scope,
                    "after": after,
                    "limit": limit,
                },
            )
            self.jobs.setdefault(scope, set()).update(job["job_id"] for job in result["jobs"])
            self.save_checkpoint()
        return result | {"scope": scope, "status_source": "controller_metadata"}

    def list_exports(self, authorization: Annotated[str | None, Header()] = None):
        actor = self.owner(authorization)
        exports = {}
        for scope in reversed(self.research_read_scopes(actor)):
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
            self.accessible_export(actor, export_id)
            return self.remote(
                "export-revoke",
                {"experiment_id": actor, "export_id": export_id, "recipient": session_id},
            )

    def delete_export(self, export_id: str, authorization: Annotated[str | None, Header()] = None):
        actor = self.owner(authorization)
        return self.remote("export-delete", {"experiment_id": actor, "export_id": export_id})

    def status(self, job_id: str, authorization: Annotated[str | None, Header()] = None):
        actor = self.owner(authorization)
        return self.remote(
            "status",
            {"experiment_id": self.job_owner(actor, job_id, shared_read=True), "job_id": job_id},
        )

    def cancel(self, job_id: str, authorization: Annotated[str | None, Header()] = None):
        actor = self.owner(authorization)
        return self.remote(
            "cancel", {"experiment_id": self.job_owner(actor, job_id), "job_id": job_id}
        )

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
