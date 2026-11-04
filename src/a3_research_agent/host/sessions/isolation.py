"""Host-owned peer jobs for repeatable cross-experiment permission checks."""

import base64
import hashlib
import json
import secrets
import threading
import uuid
from collections.abc import Callable
from pathlib import Path


class IsolationFixtures:
    def __init__(self, remote_call: Callable, root: Path):
        self.remote_call = remote_call
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.records: dict[str, dict] = {}
        self.lock = threading.RLock()

    def create(self, owner: str) -> dict:
        with self.lock:
            if any(record["owner"] == owner for record in self.records.values()):
                raise RuntimeError("Close the existing isolation fixture before creating another")
            fixture_id = uuid.uuid4().hex
            peer = "fixture-" + fixture_id
            sentinel = secrets.token_hex(32)
            result = self.remote_call(
                "submit",
                {
                    "experiment_id": peer,
                    "command": ["python", "-c", "import time; time.sleep(600)"],
                    "gpu": False,
                    "files": {"sentinel.txt": sentinel},
                },
            )
            if not result.get("job_id") or result.get("error"):
                raise RuntimeError("Isolation fixture could not acquire execution resources")
            record = {
                "owner": owner,
                "peer": peer,
                "peer_job_id": result["job_id"],
                "sentinel_sha256": hashlib.sha256(sentinel.encode()).hexdigest(),
            }
            self.records[fixture_id] = record
            (self.root / f"{fixture_id}.json").write_text(json.dumps(record, indent=2))
            return {
                "fixture_id": fixture_id,
                "peer_job_id": result["job_id"],
                "peer_artifact_path": "sentinel.txt",
                "sentinel_sha256": record["sentinel_sha256"],
            }

    def owned(self, owner: str, fixture_id: str) -> dict:
        record = self.records.get(fixture_id)
        if record is None or record["owner"] != owner:
            raise PermissionError("Fixture is not accessible")
        return record

    def verify(self, owner: str, fixture_id: str) -> dict:
        with self.lock:
            record = self.owned(owner, fixture_id)
            payload = {"experiment_id": record["peer"], "job_id": record["peer_job_id"]}
            status = self.remote_call("status", payload)
            artifact = self.remote_call("artifact", payload | {"path": "sentinel.txt"})
            if artifact.get("error") or "data" not in artifact:
                raise RuntimeError("Peer sentinel could not be verified")
            checksum = hashlib.sha256(base64.b64decode(artifact["data"], validate=True)).hexdigest()
            result = {
                "fixture_id": fixture_id,
                "peer_status": status.get("status"),
                "peer_job_intact": status.get("status") in {"created", "running"},
                "sentinel_unchanged": checksum == record["sentinel_sha256"],
            }
            (self.root / f"{fixture_id}-verification.json").write_text(json.dumps(result, indent=2))
            return result

    def cancel(self, owner: str, fixture_id: str) -> dict:
        with self.lock:
            record = self.owned(owner, fixture_id)
            result = self.remote_call(
                "cancel", {"experiment_id": record["peer"], "job_id": record["peer_job_id"]}
            )
            if result.get("status") == "error":
                raise RuntimeError("Peer job cleanup failed")
            del self.records[fixture_id]
            return {"fixture_id": fixture_id, "status": "closed"}

    def close_owner(self, owner: str | None = None) -> None:
        errors = []
        with self.lock:
            for fixture_id, record in list(self.records.items()):
                if owner is not None and record["owner"] != owner:
                    continue
                try:
                    self.cancel(record["owner"], fixture_id)
                except Exception:  # noqa: BLE001 — try every remaining fixture before reporting
                    errors.append(fixture_id)
        if errors:
            raise RuntimeError("Fixture cleanup pending: " + ", ".join(errors))

    def close(self) -> None:
        self.close_owner()
