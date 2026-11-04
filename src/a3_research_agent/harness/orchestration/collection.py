"""Worker coordination: collection."""

import base64
import json
import pathlib
import re
from urllib.parse import urlencode

from a3_research_agent.harness.context import manager as context_manager
from a3_research_agent.harness.orchestration.common import HostRequestFailed, checked, digest
from a3_research_agent.harness.orchestration.contracts import relative_path
from a3_research_agent.harness.orchestration.store import save


def artifact(req, session, name, *, bounded=False):
    query = {"path": name}
    if bounded:
        query["limit"] = 1024 * 1024 + 1  # Detect overflow without fetching an unbounded file.
    result = checked(req, "GET", "/sessions/" + session + "/artifacts?" + urlencode(query))
    raw = base64.b64decode(result["data"], validate=True)
    if len(raw) > 1024 * 1024:
        raise ValueError("Worker report too large; use bounded reports and sealed exports")
    return raw


def collect_child_artifacts(req, state, arguments):
    """Recover research outputs from a collected worker's own stopped descendants.

    Reauthorize every read with Host; local archive names never grant access. A
    separate receipt supplements delivery, without rewriting the producer's report
    or turning its prior blocked/follow-up decision into acceptance.
    """
    identifier = arguments.get("id")
    if not isinstance(identifier, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier
    ):
        raise ValueError("Expected an existing bounded assignment id")
    item = state["assignments"].get(identifier)
    if not item or item.get("status") not in {"awaiting_decision", "decided"}:
        raise ValueError("Collect the original worker report before supplemental artifacts")
    producer = item.get("session_id")
    source = arguments.get("source_session_id")
    if not isinstance(source, str) or not re.fullmatch(r"candidate-[a-f0-9]{32}", source):
        raise ValueError("Expected an explicit candidate source session")
    references = arguments.get("artifacts")
    if not isinstance(references, list) or not 1 <= len(references) <= 16:
        raise ValueError("Supplemental collection needs 1..16 research artifacts")
    selected = {}
    for reference in references:
        if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
            raise ValueError("Artifact needs only its exact research path and SHA256")
        name, expected = reference["path"], reference["sha256"]
        if (
            relative_path(name) != name
            or not name.startswith("research/")
            or "\\" in name
            or not isinstance(expected, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected)
            or name in selected
        ):
            raise ValueError("Select unique canonical research paths with full SHA256 pins")
        selected[name] = expected
    # GET /sessions is already filtered by the caller's Host ancestry ACL. Also
    # require the narrower original producer subtree, not just any main descendant.
    sessions = checked(req, "GET", "/sessions").get("sessions", [])
    parents = {x["session_id"]: x.get("parent") for x in sessions}
    chain, cursor = [], source
    while cursor != producer:
        if cursor not in parents or cursor in chain or len(chain) >= 128:
            raise ValueError("Artifact source must descend from the recorded producer worker")
        chain.append(cursor)
        cursor = parents[cursor]
    if not chain or producer not in parents:
        raise ValueError("Artifact source must be a producer descendant, not the producer itself")
    if checked(req, "GET", "/sessions/" + source).get("status") not in {"exited", "closed"}:
        raise ValueError("Wait for the artifact-producing session to stop before collection")
    batches = item.get("artifact_collections", [])
    key = digest(json.dumps([source, selected], sort_keys=True).encode())
    existing = next((x for x in batches if x.get("collection_sha256") == key), None)
    if existing is None and len(batches) >= 16:
        raise ValueError("Supplemental collection receipt limit reached")
    pending, paths, total = {}, {}, 0
    for name, expected in selected.items():
        raw = artifact(req, source, name, bounded=True)
        raw.decode("utf-8")  # Existing prepared/worker interfaces are text-only.
        total += len(raw)
        if digest(raw) != expected or total > 8 * 1024 * 1024:
            raise ValueError("Supplemental artifact SHA256 or total size mismatch")
        target = (
            pathlib.Path("orchestration/results") / identifier / "child-artifacts" / source / name
        )
        if any(p.is_symlink() for p in (target, *target.parents)):
            raise ValueError("Supplemental artifact destination must not contain symlinks")
        if target.exists() and (not target.is_file() or target.read_bytes() != raw):
            raise ValueError("Supplemental artifact conflicts with preserved local evidence")
        paths[name] = target.as_posix()
        pending[target] = raw
    # Validate all reads before writing. Publish the ledger receipt last: a write
    # failure can leave exact unreferenced files, never a false successful receipt.
    for target, raw in pending.items():
        if not target.exists():
            context_manager.atomic(target, raw)
    receipt = {
        "collection_sha256": key,
        "source_session": source,
        "producer_session": producer,
        "ancestry": chain + [producer],
        "evidence_paths": paths,
        "evidence_hashes": selected,
        "bytes": total,
        "generation_proven": False,
        "scope": "Verified supplemental bytes; not execution attestation or scientific acceptance",
    }
    if existing is not None:
        if existing != receipt:
            raise ValueError("Supplemental collection conflicts with its preserved receipt")
        return receipt
    item["artifact_collections"] = [*batches, receipt]
    save(state)
    return receipt


def collect_report_references(req, session, identifier, report_texts, declared):
    """Copy bounded, hash-addressed tool archives cited by this worker's reports.

    Optional citations are not declared evidence or scientific acceptance. Keep
    their availability explicit without inventing local files or blocking a
    truthful partial delivery. Never fetch arbitrary paths or conversation snapshots.
    """
    references = sorted(
        set(re.findall(r"context-history/tool-output-[0-9a-f]{64}\.json", "\n".join(report_texts)))
        - set(declared)
    )
    hashes, delivery, remaining = {}, {}, 1024 * 1024
    if len(references) > 8:
        delivery["__omitted__"] = {"status": "limit_exceeded", "count": len(references) - 8}
    # At most eight existing artifact requests (each <=1 MiB admitted by artifact),
    # and at most 1 MiB of verified copies. Transport is bounded per request,
    # not misrepresented as a shared 1 MiB network quota.
    for name in references[:8]:
        if remaining <= 0:
            delivery[name] = {"status": "limit_exceeded"}
            continue
        try:
            raw = artifact(req, session, name)
            expected = pathlib.PurePosixPath(name).stem.rsplit("-", 1)[-1]
            if digest(raw) != expected or len(raw) > remaining:
                raise ValueError("Unverified optional report archive")
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("type") != "function_call_output":
                raise ValueError("Report reference is not a tool-output archive")
        except (HostRequestFailed, ValueError, KeyError):
            delivery[name] = {"status": "unavailable"}
            continue
        target = pathlib.Path("orchestration/results") / identifier / name
        context_manager.atomic(target, raw.decode("utf-8"))
        remaining -= len(raw)
        hashes[name] = expected
        delivery[name] = {
            "status": "verified_copy",
            "path": target.as_posix(),
            "source_session": session,
            "sha256": expected,
        }
    return hashes, delivery


def collect(req, state, identifier):
    item = state["assignments"][identifier]
    if item["status"] == "decided":
        # An explicit review can detect changed evidence without turning a closed
        # decision back into an open task or rewriting the earlier conclusion.
        for name, expected in item.get("evidence_hashes", {}).items():
            if digest(artifact(req, item["session_id"], name)) != expected:
                raise ValueError("Worker evidence hash mismatch; prior decision remains recorded")
        return item
    session = item.get("session_id")
    if not session:
        raise ValueError("Creation uncertain: reconcile /sessions before any new submission")
    actual_task = json.loads(artifact(req, session, "worker-task.json"))
    if digest(json.dumps(actual_task, sort_keys=True, ensure_ascii=False).encode()) != item.get(
        "task_sha256"
    ):
        raise ValueError("Worker task identity differs; do not consume another assignment's result")
    status = checked(req, "GET", "/sessions/" + session)
    if status.get("status") not in {"exited", "closed"}:
        return {**item, "worker_status": status.get("status"), "decision_ready": False}
    try:
        report = json.loads(artifact(req, session, "worker-result.json"))
    except HostRequestFailed as error:
        if error.status not in (404, 422):
            raise
        item.update(
            status="awaiting_decision",
            delivery_failed=True,
            scientific_success=None,
            evidence_hashes={},
            worker_status=status.get("status"),
            worker_exit_code=status.get("exit_code"),
            failure="Worker exited without a readable required report; remote assets remain unknown",
        )
        save(state)
        return item
    if (
        not isinstance(report, dict)
        or set(report) - {"id", "role", "status", "summary", "evidence", "exports"}
        or not isinstance(report.get("summary"), str)
        or len(report["summary"]) > 4000
    ):
        raise ValueError("Worker needs a bounded report; place detailed logs in evidence files")
    if (
        report.get("id") != identifier
        or report.get("role") != item["role"]
        or report.get("status") not in {"complete", "blocked"}
    ):
        raise ValueError("Worker result does not match its bounded assignment")
    evidence = report.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > 16:
        raise ValueError("Worker result needs bounded evidence references")
    hashes = {}
    report_texts = []
    for reference in evidence:
        name = relative_path(reference["path"])
        raw = artifact(req, session, name)
        if reference.get("sha256") != digest(raw):
            raise ValueError("Worker evidence hash mismatch; no adoption decision recorded")
        target = pathlib.Path("orchestration/results") / identifier / name
        context_manager.atomic(target, raw.decode("utf-8"))
        hashes[name] = digest(raw)
        if name.endswith((".md", ".json", ".txt")):
            report_texts.append(raw.decode("utf-8"))
    reference_hashes, reference_delivery = collect_report_references(
        req, session, identifier, report_texts, hashes
    )
    if report["status"] == "complete" and not set(item["deliverables"]) <= hashes.keys():
        raise ValueError("Worker completion omits required deliverables")
    exports = report.get("exports", [])
    if not isinstance(exports, list) or len(exports) > 64:
        raise ValueError("Invalid returned exports")
    for export in exports:
        if not isinstance(export, str) or not re.fullmatch(r"export-[a-f0-9]{32}", export):
            raise ValueError("Invalid returned export ID")
        if checked(req, "GET", "/jobs/exports/" + export).get("status") != "ready":
            raise ValueError("Returned export is not ready and explicitly accessible to main")
    item.update(
        status="awaiting_decision",
        worker_status=status.get("status"),
        result=report,
        evidence_hashes=hashes,
        reference_hashes=reference_hashes,
        reference_delivery=reference_delivery,
        evidence_paths={
            name: (pathlib.PurePosixPath("orchestration/results") / identifier / name).as_posix()
            for name in {**hashes, **reference_hashes}
        },
        evidence_source_session=session,
        exports=exports,
    )
    save(state)
    return item
