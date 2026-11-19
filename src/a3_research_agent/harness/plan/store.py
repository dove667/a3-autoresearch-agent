"""Persist one project plan and immutable, run-specific revision snapshots."""

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def project_plan_file(project: Path) -> Path:
    """Locate the shared plan, including checkouts awaiting the directory move."""
    current = checked_path(project / ".runtime/research-plan/current.json")
    if current.exists():
        return current
    legacy = checked_path(project / "plan/current.json")
    return legacy if legacy.exists() else current


def checked_path(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Research plan storage requires absolute paths")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Research plan storage cannot use symbolic links")
    return path


def decode(raw: bytes) -> dict:
    plan = json.loads(raw)
    if (
        not isinstance(plan, dict)
        or type(plan.get("version")) is not int
        or plan["version"] != 2
        or type(plan.get("revision")) is not int
        or plan["revision"] < 0
        or not isinstance(plan.get("steps"), dict)
        or "goal" in plan
        or not isinstance(plan.get("direction"), dict)
    ):
        raise ValueError("Unsupported research plan structure")
    return plan


def load(plan_file: Path) -> dict:
    path = checked_path(plan_file)
    try:
        return decode(path.read_bytes())
    except FileNotFoundError:
        return {
            "version": 2,
            "revision": 0,
            "direction": {"objective": "", "metrics": [], "gaps": []},
            "routes": {},
            "reason": "",
            "steps": {},
        }


@contextmanager
def locked(plan_file: Path):
    """Serialize shared-plan reads and revision checks before a tool update."""
    path = checked_path(plan_file)
    lock = checked_path(path.parent / ".runtime/plan.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def publish(path: Path, raw: bytes, *, immutable: bool = False) -> None:
    """Publish complete bytes atomically, preserving an existing immutable file."""
    path = checked_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".plan-", dir=path.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            try:
                os.link(staged, path)
            except FileExistsError:
                if checked_path(path).read_bytes() != raw:
                    raise ValueError("Research plan snapshot content changed") from None
        else:
            os.replace(staged, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        staged.unlink(missing_ok=True)


def snapshot(plan_file: Path, snapshots_root: Path) -> Path | None:
    """Archive one complete atomic-file observation without changing its source."""
    try:
        raw = checked_path(plan_file).read_bytes()
    except FileNotFoundError:
        return None
    plan = decode(raw)
    digest = hashlib.sha256(raw).hexdigest()
    target = checked_path(snapshots_root) / f"revision-{plan['revision']:06d}-{digest}.json"
    publish(target, raw, immutable=True)
    return target


def save(plan_file: Path, plan: dict, snapshots_root: Path) -> Path:
    """Commit a revision while the caller holds the shared-plan lock."""
    raw = json.dumps(plan, ensure_ascii=False).encode()
    decode(raw)
    publish(plan_file, raw)
    return snapshot(plan_file, snapshots_root)
