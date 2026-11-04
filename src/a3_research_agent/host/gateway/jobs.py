"""Jobs Gateway behavior on the shared runtime instance."""

from typing import Annotated, Literal

from fastapi import Header, HTTPException

from .schemas import (
    ExportGrantRequest,
    ExportRequest,
    JobRequest,
)


class JobRoutes:
    def submit(self, body: JobRequest, authorization: Annotated[str | None, Header()] = None):
        with self.session_lock:
            self.accepting_resources()
            experiment = self.owner(authorization)
            request = body.model_dump(exclude_none=True)
            if not body.input_exports:
                request.pop("input_exports")
            result = self.remote("submit", request | {"experiment_id": experiment})
            if result.get("job_id"):
                self.jobs.setdefault(experiment, set()).add(result["job_id"])
                self.save_checkpoint()
            return result

    def list_exports(self, authorization: Annotated[str | None, Header()] = None):
        return self.remote("exports-list", {"experiment_id": self.owner(authorization)})

    def publish_export(
        self,
        job_id: str,
        body: ExportRequest,
        authorization: Annotated[str | None, Header()] = None,
    ):
        self.accepting_resources()
        return self.remote(
            "export-create",
            {**body.model_dump(), "experiment_id": self.owner(authorization), "job_id": job_id},
        )

    def export_status(self, export_id: str, authorization: Annotated[str | None, Header()] = None):
        return self.remote(
            "export-status", {"experiment_id": self.owner(authorization), "export_id": export_id}
        )

    def export_manifest(
        self, export_id: str, authorization: Annotated[str | None, Header()] = None
    ):
        return self.remote(
            "export-manifest", {"experiment_id": self.owner(authorization), "export_id": export_id}
        )

    def require_export_recipient(self, actor, session_id):
        if self.evolution is None:
            raise HTTPException(403, "Only the current main Agent can share exports")
        # Workers can deliver results to the current main without gaining sibling access.
        if actor in self.children and session_id == self.evolution.main_owner:
            return
        self.deployment_call(self.evolution.require_main, actor)
        record = self.children.get(session_id)
        if not record or record["parent"] != actor or record["closed"]:
            raise HTTPException(403, "Export recipient must be an active direct worker")

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
            return self.remote(
                "export-revoke",
                {"experiment_id": actor, "export_id": export_id, "recipient": session_id},
            )

    def delete_export(self, export_id: str, authorization: Annotated[str | None, Header()] = None):
        actor = self.owner(authorization)
        return self.remote("export-delete", {"experiment_id": actor, "export_id": export_id})

    def status(self, job_id: str, authorization: Annotated[str | None, Header()] = None):
        return self.remote("status", {"experiment_id": self.owner(authorization), "job_id": job_id})

    def cancel(self, job_id: str, authorization: Annotated[str | None, Header()] = None):
        return self.remote("cancel", {"experiment_id": self.owner(authorization), "job_id": job_id})

    def artifact(
        self,
        job_id: str,
        path: str,
        authorization: Annotated[str | None, Header()] = None,
        area: Literal["workspace", "outputs"] = "workspace",
    ):
        payload = {"experiment_id": self.owner(authorization), "job_id": job_id, "path": path}
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
