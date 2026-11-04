"""Host-authorized readonly export operations and sealer startup."""

import fcntl
import subprocess

from a3_research_agent.execution.controller.entry import controller_command
from a3_research_agent.execution.controller.files import safe_path
from a3_research_agent.execution.controller.store import JobStore
from a3_research_agent.execution.scheduling.gpu import active_jobs

from .readonly import ReadonlyExports


def export_operation(store: JobStore, request: dict, args) -> dict:
    """Only the Host invokes these operations; Gateway owns role/recipient checks."""
    exports = ReadonlyExports(store.root)
    actor = request.get("experiment_id")
    if not isinstance(actor, str) or not actor:
        raise PermissionError("Export is not accessible")
    operation = args.operation
    identifier = request.get("export_id")
    if operation == "exports-list":
        return {"exports": exports.list_accessible(actor)}
    if operation == "exports-check":
        ids = request.get("export_ids", [])
        if not isinstance(ids, list) or len(ids) > 64:
            raise ValueError("Expected an export ID list")
        for value in ids:
            exports.data_path(actor, value)
        return {"exports": [exports.read(actor, value) for value in ids]}
    if operation == "export-status":
        exports.read(actor, identifier)
        exports.refresh_sealer(identifier)
        return exports.read(actor, identifier)
    if operation == "export-manifest":
        return {"export_id": identifier, "files": exports.manifest(actor, identifier)}
    with (store.root / "locks/scheduler.lock").open("a") as scheduler:
        fcntl.flock(scheduler, fcntl.LOCK_EX)
        if operation == "export-create":
            job = store.load(request.get("job_id"), actor)
            location = request.get("location", "outputs")
            if location not in {"outputs", "workspace"}:
                raise ValueError("Expected outputs or workspace location")
            base = store.outputs(job) if location == "outputs" else store.workspace(job)
            source = safe_path(base, request.get("path"))
            record = exports.create(
                actor, job.get("run_id", actor), source, job["job_id"], location, request["path"]
            )
            if args.run_storage_bytes:
                planned = record["planned_bytes"]
                if store.run_storage_bytes(record["run_id"]) + planned >= args.run_storage_bytes:
                    exports.fail(record["export_id"], "run storage limit prevents export snapshot")
                    return exports.read(actor, record["export_id"])
            command = controller_command(
                "--seal-export",
                "--root",
                str(store.root),
                "--export-id",
                record["export_id"],
                "--storage-limit",
                str(args.run_storage_bytes),
            )
            try:
                with (exports.records / f"{record['export_id']}.log").open("ab") as log:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                exports.set_pid(record["export_id"], process.pid)
            except OSError:
                exports.fail(record["export_id"], "Export sealer could not start")
                raise
            return exports.read(actor, record["export_id"])
        if operation == "export-grant":
            return exports.grant(actor, identifier, request.get("recipient"))
        if operation in {"export-revoke", "export-delete"}:
            mounted = active_jobs(store, input_export=identifier)
            if operation == "export-revoke":
                mounted = [
                    job for job in mounted if job["experiment_id"] == request.get("recipient")
                ]
            if mounted:
                raise ValueError(
                    "Export is mounted by active jobs; wait before revoking or deleting"
                )
            if operation == "export-revoke":
                return exports.revoke(actor, identifier, request.get("recipient"))
            return exports.delete(actor, identifier)
    raise ValueError("Unknown export operation")
