"""Sealed binary exports grant bounded read access across worker identities."""

import json
import os

import pytest
from remote_test_support import patch_controller

from a3_research_agent.execution.controller import store as job_store
from a3_research_agent.execution.exports import operations as job_exports
from a3_research_agent.execution.exports.readonly import ReadonlyExports


def test_binary_snapshot_is_immutable_and_requires_explicit_grant(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    payload = b"\x00\xff" * (17 * 1024 * 1024)
    (source / "model.bin").write_bytes(payload)
    exports = ReadonlyExports(tmp_path / "runtime")
    record = exports.create("main", "run-one", source, "job-one", "outputs", "checkpoint")
    exports.seal(record["export_id"])
    ready = exports.read("main", record["export_id"])
    assert ready["status"] == "ready"
    assert ready["size_bytes"] == len(payload)
    with pytest.raises(PermissionError):
        exports.read("next", record["export_id"])
    exports.grant("main", record["export_id"], "next")
    assert (
        exports.data_path("next", record["export_id"]).joinpath("model.bin").read_bytes() == payload
    )
    (source / "model.bin").write_bytes(b"new producer state")
    assert (
        exports.data_path("next", record["export_id"]).joinpath("model.bin").read_bytes() == payload
    )
    manifest = exports.manifest("next", record["export_id"])
    assert manifest[0]["size_bytes"] == len(payload)
    assert len(manifest[0]["sha256"]) == 64
    assert exports.list_accessible("unrelated") == []
    exports.grant("next", record["export_id"], "third")
    exports.revoke("main", record["export_id"], "third")
    with pytest.raises(PermissionError):
        exports.data_path("third", record["export_id"])
    # Reopening a Host preserves the grants and snapshot.
    assert (
        ReadonlyExports(exports.root).read("next", record["export_id"])["sha256"] == ready["sha256"]
    )


def test_exports_reject_symlinks_and_unready_or_forged_reads(tmp_path):
    exports = ReadonlyExports(tmp_path / "runtime")
    source = tmp_path / "source"
    source.mkdir()
    (source / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError):
        exports.create("main", "run-one", source, "j", "outputs", "checkpoint")
    (source / "link").unlink()
    (source / "file").write_text("content")
    record = exports.create("main", "run-one", source, "j", "outputs", "checkpoint")
    with pytest.raises(ValueError):
        exports.data_path("main", record["export_id"])
    for identifier in ("../private", "export-" + "0" * 32):
        with pytest.raises(PermissionError):
            exports.read("other", identifier)


def test_sealing_failure_releases_partial_files_and_keeps_failure_record(tmp_path):
    exports = ReadonlyExports(tmp_path / "runtime")
    source = tmp_path / "model.bin"
    source.write_bytes(b"x" * 2048)
    record = exports.create("main", "run-one", source, "j", "outputs", "model.bin")
    exports.seal(record["export_id"], storage_usage=lambda: 4096, storage_limit=1024)
    failed = exports.read("main", record["export_id"])
    assert failed["status"] == "failed"
    assert "storage" in failed["error"]
    assert not exports.directory(record["export_id"]).joinpath("data").exists()
    assert source.read_bytes() == b"x" * 2048


def test_changed_source_and_interrupted_sealer_are_diagnosed(tmp_path, monkeypatch):
    exports = ReadonlyExports(tmp_path / "runtime")
    source = tmp_path / "model.bin"
    source.write_bytes(b"original")
    record = exports.create("main", "run-one", source, "j", "outputs", "model.bin")
    source.write_bytes(b"changed file length")
    exports.seal(record["export_id"])
    assert "source changed" in exports.read("main", record["export_id"])["error"]

    interrupted = exports.create("main", "run-one", source, "j", "outputs", "model.bin")
    exports.set_pid(interrupted["export_id"], 99999999)

    def missing_process(pid, signal):
        raise ProcessLookupError

    monkeypatch.setattr("a3_research_agent.execution.exports.readonly.os.kill", missing_process)
    exports.refresh_sealer(interrupted["export_id"])
    assert exports.read("main", interrupted["export_id"])["status"] == "failed"


def test_failure_after_data_rename_removes_unpublished_binary(tmp_path, monkeypatch):
    exports = ReadonlyExports(tmp_path / "runtime")
    source = tmp_path / "model.bin"
    source.write_bytes(b"complete copy")
    record = exports.create("main", "run-one", source, "j", "outputs", "model.bin")
    save = exports.save

    def fail_ready_metadata(value):
        if value["status"] == "ready":
            raise OSError("controlled publication failure")
        save(value)

    monkeypatch.setattr(exports, "save", fail_ready_metadata)
    exports.seal(record["export_id"])
    assert exports.read("main", record["export_id"])["status"] == "failed"
    assert not exports.directory(record["export_id"]).joinpath("data").exists()


def test_path_replacement_at_open_cannot_publish_unselected_contents(tmp_path, monkeypatch):
    exports = ReadonlyExports(tmp_path / "runtime")
    source = tmp_path / "model.bin"
    source.write_bytes(b"selected contents")
    other = tmp_path / "other.bin"
    other.write_bytes(b"unselected contents")
    original = tmp_path / "original.bin"
    record = exports.create("main", "run-one", source, "j", "outputs", "model.bin")
    real_open = os.open

    def swap_before_open(path, flags, *args, **kwargs):
        if path == source.name and kwargs.get("dir_fd") is not None:
            source.rename(original)
            source.symlink_to(other)
            try:
                return real_open(path, flags, *args, **kwargs)
            finally:
                source.unlink()
                original.rename(source)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    exports.seal(record["export_id"])
    assert exports.read("main", record["export_id"])["status"] == "failed"
    assert source.read_bytes() == b"selected contents"


def test_small_fast_copy_still_checks_storage_before_publication(tmp_path):
    exports = ReadonlyExports(tmp_path / "runtime")
    source = tmp_path / "model.bin"
    source.write_bytes(b"x" * 2048)
    record = exports.create("main", "run-one", source, "j", "outputs", "model.bin")
    measurements = iter([2048, 4096])
    exports.seal(record["export_id"], lambda: next(measurements), 3072)
    assert exports.read("main", record["export_id"])["status"] == "failed"
    assert not exports.directory(record["export_id"]).joinpath("data").exists()


def test_manifest_bytes_count_toward_quota_and_are_removed_on_failure(tmp_path):
    from a3_research_agent.execution.controller.store import JobStore

    store = JobStore(tmp_path / "runtime")
    exports = ReadonlyExports(store.root)
    job = store.create("main", ["true"], False, {}, run_id="run-one")
    source = store.outputs(job)
    for number in range(100):
        (source / str(number)).write_bytes(b"x")
    record = exports.create("main", "run-one", source, job["job_id"], "outputs", ".")
    exports.seal(record["export_id"], lambda: store.run_storage_bytes("run-one"), 500)
    assert exports.read("main", record["export_id"])["status"] == "failed"
    assert not (exports.directory(record["export_id"]) / "manifest.json").exists()
    assert store.run_storage_bytes("run-one") == 100


def test_failure_record_write_error_does_not_leave_large_snapshot(tmp_path, monkeypatch):
    exports = ReadonlyExports(tmp_path / "runtime")
    source = tmp_path / "model.bin"
    source.write_bytes(b"checkpoint")
    record = exports.create("main", "run-one", source, "j", "outputs", "model.bin")
    directory = exports.directory(record["export_id"])
    (directory / "staging").mkdir()
    (directory / "staging/partial.bin").write_bytes(b"partial contents")
    (directory / "manifest.json").write_text("[]")

    def full_disk(value):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(exports, "save", full_disk)
    with pytest.raises(OSError):
        exports.fail(record["export_id"], "disk full")
    assert list(directory.iterdir()) == []


def test_atomic_json_failure_cleans_temporary_file(tmp_path, monkeypatch):
    from a3_research_agent.execution.exports import readonly as readonly_exports

    def failed_dump(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(readonly_exports.json, "dump", failed_dump)
    with pytest.raises(OSError):
        readonly_exports.write_json(tmp_path / "manifest.json", {})
    assert list(tmp_path.iterdir()) == []


def test_revoke_and_delete_refuse_live_consumers(tmp_path, monkeypatch):
    from argparse import Namespace

    store = job_store.JobStore(tmp_path / "runtime")
    exports = ReadonlyExports(store.root)
    source = tmp_path / "model.bin"
    source.write_bytes(b"checkpoint")
    record = exports.create("main", "run-one", source, "j", "outputs", "model.bin")
    identifier = record["export_id"]
    exports.seal(identifier)
    exports.grant("main", identifier, "worker")
    active = [{"experiment_id": "worker", "input_exports": [identifier]}]
    patch_controller(monkeypatch, "active_jobs", lambda store, **kwargs: active)
    request = {"experiment_id": "main", "export_id": identifier, "recipient": "worker"}
    for operation in ("export-revoke", "export-delete"):
        with pytest.raises(ValueError, match="active jobs"):
            job_exports.export_operation(store, request, Namespace(operation=operation))
    active.clear()
    job_exports.export_operation(store, request, Namespace(operation="export-revoke"))
    with pytest.raises(PermissionError):
        exports.read("worker", identifier)
    job_exports.export_operation(store, request, Namespace(operation="export-delete"))
    assert source.read_bytes() == b"checkpoint"


def test_retained_declaration_carries_only_validated_export_references(tmp_path):
    from a3_research_agent.host.runs.continuation import (
        load_run_result,
        prepare_fresh_task,
        publish_run_result,
    )
    from a3_research_agent.host.sessions.versions import CandidateVersions

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "agent.py").write_text("pass")
    (workspace / "checkpoint.json").write_text("[]")
    identifier = "export-" + "a" * 32
    (workspace / "continuation.json").write_text(
        json.dumps(
            {
                "command": ["python", "agent.py", "--resume"],
                "files": ["agent.py", "checkpoint.json"],
                "remote_exports": [identifier],
            }
        )
    )
    versions = CandidateVersions(tmp_path / "versions")
    calls = []
    publish_run_result(
        tmp_path, workspace, versions, "main", None, validate_exports=lambda ids: calls.append(ids)
    )
    assert calls == [[identifier]]
    start = load_run_result(tmp_path)
    assert start.remote_exports == [identifier]
    assert start.source_owner == "main"
    fresh, _ = prepare_fresh_task(start, "new task")
    assert fresh.remote_exports == [identifier]


def test_new_run_imports_explicit_retained_exports_and_rolls_back_partial_failure(tmp_path):
    from a3_research_agent.host.runs.continuation import HarnessStart, import_retained_exports

    exports = ReadonlyExports(tmp_path / "runtime")
    source = tmp_path / "model.bin"
    source.write_bytes(b"binary model")
    record = exports.create("old-main", "old-run", source, "j", "outputs", "model.bin")
    identifier = record["export_id"]
    exports.seal(identifier)
    calls = []

    def remote(operation, request):
        calls.append(operation)
        if operation == "exports-check":
            for value in request["export_ids"]:
                exports.data_path(request["experiment_id"], value)
            return {"status": "ready"}
        if operation == "export-grant":
            if len(calls) == 3:
                return {"status": "error"}
            return exports.grant(
                request["experiment_id"], request["export_id"], request["recipient"]
            )
        return exports.revoke(request["experiment_id"], request["export_id"], request["recipient"])

    start = HarnessStart(["true"], {}, source_owner="old-main", remote_exports=[identifier])
    assert import_retained_exports(start, "new-run", remote) == [identifier]
    assert (
        exports.data_path("new-run", identifier).joinpath("model.bin").read_bytes()
        == b"binary model"
    )
    start.remote_exports.append(identifier)
    calls.clear()
    with pytest.raises(ValueError, match="Cannot import"):
        import_retained_exports(start, "failed-run", remote)
    with pytest.raises(PermissionError):
        exports.data_path("failed-run", identifier)
    assert calls[-1] == "export-revoke"


def test_many_retained_assets_use_bounded_checks_before_any_grant():
    from a3_research_agent.host.runs.continuation import HarnessStart, import_retained_exports

    identifiers = ["export-" + f"{i:032x}" for i in range(130)]
    calls = []

    def remote(operation, request):
        calls.append((operation, request))
        return {"status": "ready"}

    start = HarnessStart(["true"], {}, source_owner="old-main", remote_exports=identifiers)
    assert import_retained_exports(start, "new-main", remote) == identifiers
    assert [
        len(payload["export_ids"]) for operation, payload in calls if operation == "exports-check"
    ] == [64, 64, 2]
    assert [operation for operation, _ in calls[:3]] == ["exports-check"] * 3
