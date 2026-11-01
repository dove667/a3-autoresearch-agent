"""Seal selected binary outputs and persist explicit read-only access grants."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

EXPORT_ID = re.compile(r"export-[a-f0-9]{32}\Z")
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def write_json(path: Path, value) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        with temporary.open("w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def open_source(path: Path):
    """Keep directory descriptors fixed and refuse links during every open."""
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            next_descriptor = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = next_descriptor
        source = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(source, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Export source is no longer a regular file")
            yield stream
    finally:
        os.close(descriptor)


class ReadonlyExports:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.records = self.root / "readonly-exports"
        self.records.mkdir(parents=True, exist_ok=True)
        (self.root / "locks").mkdir(exist_ok=True)

    def metadata_path(self, identifier: str) -> Path:
        if not isinstance(identifier, str) or not EXPORT_ID.fullmatch(identifier):
            raise PermissionError("Export is not accessible")
        return self.records / f"{identifier}.json"

    @contextmanager
    def lock(self, identifier: str):
        with self.metadata_path(identifier).with_suffix(".lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def load(self, identifier: str) -> dict:
        try:
            return json.loads(self.metadata_path(identifier).read_text())
        except FileNotFoundError:
            raise PermissionError("Export is not accessible") from None

    def save(self, record: dict) -> None:
        write_json(self.metadata_path(record["export_id"]), record)

    def directory(self, identifier: str) -> Path:
        record = self.load(identifier)
        return self.root / "runs" / record["run_id"] / "readonly-exports" / identifier

    @staticmethod
    def public(record: dict) -> dict:
        return {
            key: value
            for key, value in record.items()
            if key not in {"readers", "source_root", "plan", "sealer_pid"}
        }

    def read(self, actor: str, identifier: str) -> dict:
        record = self.load(identifier)
        if actor not in record["readers"] or record["status"] == "deleted":
            raise PermissionError("Export is not accessible")
        return self.public(record)

    def list_accessible(self, actor: str) -> list[dict]:
        result = []
        for path in sorted(self.records.glob("export-*.json")):
            record = json.loads(path.read_text())
            if actor in record["readers"] and record["status"] != "deleted":
                self.refresh_sealer(record["export_id"])
                result.append(self.read(actor, record["export_id"]))
        return result

    def refresh_sealer(self, identifier: str) -> None:
        """Report an interrupted copy instead of leaving it permanently preparing."""
        record = self.load(identifier)
        pid = record.get("sealer_pid")
        if record["status"] != "preparing" or not pid:
            return
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            self.fail(identifier, "Export sealer exited before completing the snapshot")
            return
        except PermissionError:
            return
        process = Path("/proc") / str(pid)
        if process.exists():
            try:
                arguments = (process / "cmdline").read_bytes()
            except FileNotFoundError:
                arguments = b""
            if b"--seal-export" not in arguments or identifier.encode() not in arguments:
                self.fail(identifier, "Export sealer is no longer running")

    @staticmethod
    def scan(source: Path) -> tuple[Path, list[dict]]:
        if source.is_symlink() or not source.exists():
            raise ValueError("Export source must exist without symlinks")
        base = source if source.is_dir() else source.parent
        paths = sorted(source.rglob("*")) if source.is_dir() else [source]
        files = []
        for path in paths:
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Exports accept regular files and directories only")
            files.append(
                {
                    "path": path.relative_to(base).as_posix(),
                    "size_bytes": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                    "inode": info.st_ino,
                }
            )
        return base.resolve(), files

    def create(
        self, actor: str, run_id: str, source: Path, job_id: str, location: str, selected_path: str
    ) -> dict:
        if not IDENTITY.fullmatch(run_id):
            raise ValueError("Invalid export run identity")
        base, files = self.scan(source)
        identifier = "export-" + uuid.uuid4().hex
        record = {
            "export_id": identifier,
            "owner": actor,
            "readers": [actor],
            "run_id": run_id,
            "source_job_id": job_id,
            "location": location,
            "path": selected_path,
            "source_root": str(base),
            "plan": files,
            "status": "preparing",
            "created_at": time.time(),
            "planned_bytes": sum(item["size_bytes"] for item in files),
            "file_count": len(files),
            "mount": "/inputs/" + identifier,
        }
        directory = self.root / "runs" / run_id / "readonly-exports" / identifier
        directory.mkdir(parents=True)
        try:
            self.save(record)
        except OSError:
            shutil.rmtree(directory)
            raise
        return self.public(record)

    def set_pid(self, identifier: str, pid: int) -> None:
        with self.lock(identifier):
            record = self.load(identifier)
            record["sealer_pid"] = pid
            self.save(record)

    def fail(self, identifier: str, error: str) -> None:
        with self.lock(identifier):
            record = self.load(identifier)
            if record["status"] in {"ready", "deleted"}:
                return
            # Release space before trying to write a failure record on a full disk.
            directory = self.directory(identifier)
            shutil.rmtree(directory / "staging", ignore_errors=True)
            shutil.rmtree(directory / "data", ignore_errors=True)
            (directory / "manifest.json").unlink(missing_ok=True)
            record.update(status="failed", error=error, finished_at=time.time())
            self.save(record)

    def seal(self, identifier: str, storage_usage=None, storage_limit: int = 0) -> None:
        record = self.load(identifier)
        if record["status"] != "preparing":
            return
        staging = self.directory(identifier) / "staging"
        staging.mkdir(exist_ok=True)
        manifest = []
        last_check = 0.0
        try:
            for item in record["plan"]:
                source = Path(record["source_root"]) / item["path"]
                # Recheck every component: the producer may have replaced a path after publication.
                for path in [source, *source.parents]:
                    if path.is_symlink():
                        raise ValueError("Export source changed to a symlink")
                    if path == Path(record["source_root"]):
                        break
                info = source.stat()
                if (info.st_size, info.st_mtime_ns, info.st_ino) != (
                    item["size_bytes"],
                    item["mtime_ns"],
                    item["inode"],
                ):
                    raise ValueError("Export source changed before sealing")
                target = staging / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with open_source(source) as incoming, target.open("wb") as outgoing:
                    opened = os.fstat(incoming.fileno())
                    if (opened.st_size, opened.st_mtime_ns, opened.st_ino) != (
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ino,
                    ):
                        raise ValueError("Export source changed before opening")
                    while True:
                        if storage_limit and time.monotonic() - last_check >= 1:
                            if storage_usage() >= storage_limit:
                                raise ValueError("run storage limit reached during export")
                            last_check = time.monotonic()
                        chunk = incoming.read(1024 * 1024)
                        if not chunk:
                            break
                        outgoing.write(chunk)
                        digest.update(chunk)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                    opened_after = os.fstat(incoming.fileno())
                    if (opened_after.st_size, opened_after.st_mtime_ns, opened_after.st_ino) != (
                        opened.st_size,
                        opened.st_mtime_ns,
                        opened.st_ino,
                    ):
                        raise ValueError("Export source changed during copying")
                current = source.stat()
                if (current.st_size, current.st_mtime_ns, current.st_ino) != (
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ino,
                ):
                    raise ValueError("Export source changed while sealing")
                manifest.append(
                    {
                        "path": item["path"],
                        "size_bytes": target.stat().st_size,
                        "sha256": digest.hexdigest(),
                    }
                )
            with (self.root / "locks/scheduler.lock").open("a") as scheduler:
                fcntl.flock(scheduler, fcntl.LOCK_EX)
                write_json(self.directory(identifier) / "manifest.json", manifest)
                if storage_limit and storage_usage() >= storage_limit:
                    raise ValueError("run storage limit reached before export publication")
                staging.replace(self.directory(identifier) / "data")
                sync_directory(self.directory(identifier))
                with self.lock(identifier):
                    record = self.load(identifier)
                    record.update(
                        status="ready",
                        size_bytes=sum(x["size_bytes"] for x in manifest),
                        sha256=hashlib.sha256(
                            json.dumps(manifest, sort_keys=True).encode()
                        ).hexdigest(),
                        finished_at=time.time(),
                    )
                    self.save(record)
        except (OSError, ValueError) as error:
            self.fail(identifier, str(error))

    def data_path(self, actor: str, identifier: str) -> Path:
        record = self.read(actor, identifier)
        if record["status"] != "ready":
            raise ValueError("Export is not ready")
        return self.directory(identifier) / "data"

    def manifest(self, actor: str, identifier: str) -> list[dict]:
        self.data_path(actor, identifier)
        return json.loads((self.directory(identifier) / "manifest.json").read_text())

    def grant(self, actor: str, identifier: str, recipient: str) -> dict:
        if not isinstance(recipient, str) or not IDENTITY.fullmatch(recipient):
            raise ValueError("Invalid recipient")
        with self.lock(identifier):
            self.data_path(actor, identifier)
            record = self.load(identifier)
            if recipient not in record["readers"]:
                record["readers"].append(recipient)
                self.save(record)
        return {"export_id": identifier, "recipient": recipient, "access": "read_only"}

    def revoke(self, actor: str, identifier: str, recipient: str) -> dict:
        with self.lock(identifier):
            self.read(actor, identifier)
            record = self.load(identifier)
            if recipient == record["owner"]:
                raise ValueError("Cannot revoke the source owner's access")
            record["readers"] = [reader for reader in record["readers"] if reader != recipient]
            self.save(record)
        return {"export_id": identifier, "recipient": recipient, "access": "revoked"}

    def delete(self, actor: str, identifier: str) -> dict:
        with self.lock(identifier):
            record = self.load(identifier)
            self.read(actor, identifier)
            if actor != record["owner"]:
                raise PermissionError("Only the source owner may delete an export")
            if record["status"] == "preparing":
                raise ValueError("Wait for export sealing before deleting")
            shutil.rmtree(self.directory(identifier))
            record.update(status="deleted", readers=[], deleted_at=time.time())
            self.save(record)
        return {"export_id": identifier, "status": "deleted"}
