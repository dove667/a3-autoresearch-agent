"""Host-private cache for already validated, successful public web results.

Keys are opaque strings and only their SHA-256 is persisted. The caller must
strip request metadata, validate public sources, and never submit credentials,
images or binary content. This module does not authorize URLs or make requests.
Use ``with cache.lock(key)`` around lookup/provider/store for singleflight;
explicit refresh bypasses lookup, not the lock. Cache failures are misses.

Disk budget counts complete JSON files (including their integrity envelopes),
not filesystem allocation blocks. Fixed zero-byte lock stripes bound metadata;
no per-request lock/index files accumulate. Host-local POSIX filesystems only.
"""

import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DEFAULT_MAX_BYTES = 500_000_000
SEARCH_TTL_SECONDS = 86_400
FETCH_TTL_SECONDS = 604_800
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
_MAX_ENTRY_BYTES = MAX_PAYLOAD_BYTES + 1024
_ENTRY = re.compile(r"[0-9a-f]{64}\.json\Z")
_STRIPES = 64
_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "headers",
        "request_id",
        "query",
        "raw_query",
        "raw_response",
        "images",
        "binary",
        "credentials",
    }
)


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _safe_payload(value):
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and key.lower() not in _FORBIDDEN_KEYS and _safe_payload(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(_safe_payload(item) for item in value)
    return value is None or isinstance(value, (str, int, float, bool))


class CacheBusy(TimeoutError):
    """Singleflight timed out: caller must not start a duplicate provider request."""


class WebCache:
    """Bounded JSON disk cache; TTL is measured from store, never from access."""

    def __init__(self, root, max_bytes=DEFAULT_MAX_BYTES, *, lock_timeout=15.0):
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
            raise ValueError("cache max_bytes must be a nonnegative integer")
        if not math.isfinite(lock_timeout) or lock_timeout < 0:
            raise ValueError("cache lock_timeout must be finite and nonnegative")
        self.lock_timeout = lock_timeout
        self.warning = None  # sticky, redacted Host diagnostic, never persisted in payload
        self.root = Path(root)
        self.max_bytes = max_bytes
        self._directory = None
        if max_bytes:
            directory = None
            try:
                self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
                directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                if os.fstat(directory).st_uid != os.geteuid():
                    raise OSError("cache directory must be owned by Host user")
                os.fchmod(directory, 0o700)
                self._directory = directory
                with self._file_lock(".cache.lock"):
                    self._inventory()  # enforce a reduced Host budget on reopen
            except OSError:
                if directory is not None:
                    os.close(directory)
                self._directory = None
                self.warning = self.warning or "cache_unavailable"
                # Read-only/unavailable disks disable this optional optimization.

    def __del__(self):
        directory = getattr(self, "_directory", None)
        if directory is not None:
            os.close(directory)

    @staticmethod
    def _hash(key):
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _open(self, name, flags, mode=0o600):
        descriptor = os.open(
            name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=self._directory
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                raise OSError("cache file must be a private regular file")
            os.fchmod(descriptor, 0o600)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @contextmanager
    def _file_lock(self, name):
        descriptor = self._open(name, os.O_RDWR | os.O_CREAT)
        try:
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        self.warning = "cache_lock_timeout"
                        raise CacheBusy("cache lock timeout") from None
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
            yield
        finally:
            os.close(descriptor)  # closing also releases flock after exceptions/crashes

    @contextmanager
    def lock(self, key):
        """Cross-thread/process singleflight; no global lock during provider work.

        Hash collisions on the fixed stripes can serialize unrelated requests.
        Locks are not reentrant; never nest lock() calls.
        """
        if not self.max_bytes or self._directory is None:
            yield
            return
        stripe = int(self._hash(key)[:8], 16) % _STRIPES
        guard = self._file_lock(f".key-{stripe:02x}.lock")
        try:
            guard.__enter__()
        except CacheBusy:
            raise
        except OSError:
            self.warning = self.warning or "cache_unavailable"
            # Fail open only on acquisition, never swallow caller/provider errors.
            yield
            return
        try:
            yield
        finally:
            guard.__exit__(None, None, None)

    def _unlink(self, name):
        try:
            os.unlink(name, dir_fd=self._directory)
        except FileNotFoundError:
            pass

    def _inventory(self):
        result = []
        with os.scandir(self._directory) as items:
            for item in items:
                if item.name.startswith(".pending-"):
                    # Under the global lock, any pending file is from a crashed writer.
                    self._unlink(item.name)
                elif _ENTRY.fullmatch(item.name):
                    info = item.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        self._unlink(item.name)
                        continue
                    result.append((info.st_mtime_ns, item.name, info.st_size))
        result.sort()
        size = sum(item[2] for item in result)
        removed = 0
        for _, name, length in result:
            if size <= self.max_bytes:
                break
            self._unlink(name)
            size -= length
            removed += 1
        return result[removed:]

    def get(self, key, ttl):
        """Return a fresh decoded dict or None; expired/corrupt data is deleted."""
        if not self.max_bytes or self._directory is None or not math.isfinite(ttl) or ttl <= 0:
            return None
        name = self._hash(key) + ".json"
        try:
            with self._file_lock(".cache.lock"):
                self._inventory()
                try:
                    descriptor = self._open(name, os.O_RDONLY)
                    with os.fdopen(descriptor, "rb") as stream:
                        if os.fstat(stream.fileno()).st_size > _MAX_ENTRY_BYTES:
                            raise ValueError("oversized entry")
                        raw = stream.read(_MAX_ENTRY_BYTES + 1)
                    envelope = json.loads(raw)
                    if not isinstance(envelope, dict) or set(envelope) != {
                        "schema",
                        "key",
                        "stored_at",
                        "payload",
                        "digest",
                    }:
                        raise ValueError("invalid envelope")
                    digest = envelope.pop("digest")
                    if not isinstance(digest, str) or not hmac.compare_digest(
                        digest, hashlib.sha256(_json(envelope)).hexdigest()
                    ):
                        raise ValueError("invalid digest")
                    stored = envelope["stored_at"]
                    if (
                        envelope["schema"] != 1
                        or envelope["key"] != self._hash(key)
                        or not isinstance(stored, (int, float))
                        or not math.isfinite(stored)
                        or not self._valid_payload(envelope["payload"])
                    ):
                        raise ValueError("invalid entry")
                    if not 0 <= time.time() - stored < ttl:
                        self._unlink(name)
                        return None
                    os.utime(name, None, dir_fd=self._directory, follow_symlinks=False)
                    return envelope["payload"]
                except FileNotFoundError:
                    return None
                except (ValueError, TypeError, RecursionError):
                    self.warning = "cache_corrupt"
                    self._unlink(name)
                except OSError:
                    self.warning = self.warning or "cache_unavailable"
                    self._unlink(name)
        except (OSError, ValueError, TypeError, RecursionError):
            self.warning = self.warning or "cache_unavailable"
        return None

    @staticmethod
    def _valid_payload(payload):
        return (
            isinstance(payload, dict)
            and "error" not in payload
            and payload.get("success") is not False
            and payload.get("status") not in ("error", "failed", "failure")
            and _safe_payload(payload)
        )

    def put(self, key, payload):
        """Store only valid bounded JSON, atomically; return False on cache failure."""
        if not self.max_bytes or self._directory is None:
            return False
        try:
            if not self._valid_payload(payload) or len(_json(payload)) > MAX_PAYLOAD_BYTES:
                return False
            hashed = self._hash(key)
            envelope = {"schema": 1, "key": hashed, "stored_at": time.time(), "payload": payload}
            envelope["digest"] = hashlib.sha256(_json(envelope)).hexdigest()
            raw = _json(envelope)
            if len(raw) > self.max_bytes or len(raw) > _MAX_ENTRY_BYTES:
                return False
            name = hashed + ".json"
            with self._file_lock(".cache.lock"):
                inventory = self._inventory()
                # Remove the old version before writing so even transient bytes fit.
                # Readers share this lock; a crash here is a miss, never partial content.
                self._unlink(name)
                inventory = [item for item in inventory if item[1] != name]
                size = sum(item[2] for item in inventory)
                for _, victim, length in sorted(inventory):
                    if size + len(raw) <= self.max_bytes:
                        break
                    self._unlink(victim)
                    size -= length
                temporary = ".pending-" + uuid.uuid4().hex
                try:
                    descriptor = self._open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(
                        temporary, name, src_dir_fd=self._directory, dst_dir_fd=self._directory
                    )
                    os.fsync(self._directory)
                finally:
                    self._unlink(temporary)
            return True
        except (OSError, ValueError, TypeError, RecursionError):
            self.warning = self.warning or "cache_unavailable"
            return False
