"""Offline disk-cache contract; no network/provider requests."""

import hashlib
import json
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from runtime.local.web_cache import MAX_PAYLOAD_BYTES, CacheBusy, WebCache


def payload(text="public page"):
    return {"retrieved_at": "2026-01-01T00:00:00+00:00", "sources": [{"text": text}]}


def entries(root):
    return list(root.glob("*.json"))


def test_roundtrip_private_hashed_paths_and_independent_copies(tmp_path):
    cache = WebCache(tmp_path / "cache")
    key = "../../secret raw query"
    original = payload()
    assert cache.put(key, original)
    original["sources"][0]["text"] = "mutated"
    first = cache.get(key, 60)
    assert first == payload()
    first["sources"].clear()
    assert cache.get(key, 60) == payload()
    assert entries(cache.root)[0].name == hashlib.sha256(key.encode()).hexdigest() + ".json"
    assert key not in entries(cache.root)[0].read_text()
    assert cache.root.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in cache.root.iterdir())


def test_ttl_does_not_reset_on_read(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("runtime.local.web_cache.time.time", lambda: now[0])
    cache = WebCache(tmp_path)
    assert cache.put("key", payload())
    now[0] = 1009
    assert cache.get("key", 10) == payload()
    now[0] = 1010
    assert cache.get("key", 10) is None
    assert not entries(tmp_path)


def test_lru_eviction_and_exact_json_byte_budget(tmp_path):
    cache = WebCache(tmp_path)
    assert cache.put("a", payload())
    size = entries(tmp_path)[0].stat().st_size
    cache.max_bytes = size * 2 + 20  # timestamp representation can vary slightly
    assert cache.put("b", payload())
    assert cache.get("a", 60)
    assert cache.put("c", payload())
    assert cache.get("b", 60) is None
    assert cache.get("a", 60)
    assert cache.get("c", 60)
    assert sum(p.stat().st_size for p in tmp_path.iterdir()) <= cache.max_bytes


def test_budget_counts_envelope_not_only_payload(tmp_path):
    cache = WebCache(tmp_path, max_bytes=len(json.dumps(payload()).encode()))
    assert not cache.put("a", payload())
    assert not entries(tmp_path)


def test_disabled_cache_does_not_create_directory(tmp_path):
    cache = WebCache(tmp_path / "absent", max_bytes=0)
    with cache.lock("a"):
        assert cache.get("a", 60) is None
        assert not cache.put("a", payload())
    assert not cache.root.exists()


def test_oversize_and_sensitive_or_failure_payloads_not_stored(tmp_path):
    cache = WebCache(tmp_path)
    bad = [
        payload("x" * MAX_PAYLOAD_BYTES),
        {"error": "failed"},
        {"success": False},
        {"status": "error"},
        {"request_id": "id"},
        {"sources": [{"api_key": "secret"}]},
        {"query": "raw"},
        {"bytes": b"binary"},
        {"value": float("nan")},
    ]
    for value in bad:
        assert not cache.put("a", value)
    assert not entries(tmp_path)


def test_corrupt_schema_digest_and_future_clock_are_misses(tmp_path):
    cache = WebCache(tmp_path)
    for damage in (
        lambda d: d.update(schema=99),
        lambda d: d["payload"].update(sources=[]),
        lambda d: d.update(stored_at=time.time() + 3600),
    ):
        assert cache.put("a", payload())
        path = entries(tmp_path)[0]
        data = json.loads(path.read_text())
        damage(data)
        path.write_text(json.dumps(data))
        assert cache.get("a", 60) is None
    assert cache.put("a", payload())
    entries(tmp_path)[0].write_bytes(b"{partial")
    assert cache.get("a", 60) is None


def test_symlink_entry_never_read_or_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    cache = WebCache(tmp_path / "cache")
    path = cache.root / (hashlib.sha256(b"a").hexdigest() + ".json")
    path.symlink_to(outside)
    assert cache.get("a", 60) is None
    assert cache.put("a", payload())
    assert outside.read_text() == "untouched"


def test_thread_singleflight_across_cache_instances(tmp_path):
    calls = []
    barrier = threading.Barrier(8)

    def request(_):
        cache = WebCache(tmp_path)
        barrier.wait()
        with cache.lock("same"):
            value = cache.get("same", 60)
            if value is None:
                calls.append(1)
                time.sleep(0.01)
                value = payload()
                assert cache.put("same", value)
            return value

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(request, range(8))) == [payload()] * 8
    assert len(calls) == 1


def process_request(root, count, start):
    cache = WebCache(root)
    start.wait()
    with cache.lock("same"):
        if cache.get("same", 60) is None:
            with count.get_lock():
                count.value += 1
            time.sleep(0.02)
            assert cache.put("same", payload())


def test_process_singleflight(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    count, start = ctx.Value("i", 0), ctx.Event()
    processes = [
        ctx.Process(target=process_request, args=(tmp_path, count, start)) for _ in range(4)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert count.value == 1


def test_lock_metadata_bounded_and_crash_temporary_removed(tmp_path):
    cache = WebCache(tmp_path, max_bytes=1000)
    (tmp_path / ".pending-deadbeef").write_text("interrupted")
    for index in range(200):
        with cache.lock(str(index)):
            cache.put(str(index), payload())
    assert not list(tmp_path.glob(".pending-*"))
    assert len(list(tmp_path.iterdir())) <= 68
    assert sum(p.stat().st_size for p in tmp_path.iterdir()) <= 1000


def test_atomic_reader_writer(tmp_path):
    cache = WebCache(tmp_path)
    values = [payload("a" * 10000), payload("b" * 10000)]
    cache.put("a", values[0])

    def writer():
        for index in range(40):
            assert cache.put("a", values[index % 2])

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(writer)
        for _ in range(80):
            assert cache.get("a", 60) in values
        future.result()


def test_unavailable_root_fails_open_without_touching_other_files(tmp_path):
    root = tmp_path / "not-a-directory"
    root.write_text("keep")
    cache = WebCache(root)
    with cache.lock("a"):
        assert cache.get("a", 60) is None
        assert cache.put("a", payload()) is False
    assert root.read_text() == "keep"


def test_unavailable_singleflight_lock_fails_open_but_provider_exception_propagates(tmp_path):
    import pytest

    cache = WebCache(tmp_path)
    stripe = int(hashlib.sha256(b"a").hexdigest()[:8], 16) % 64
    outside = tmp_path / "outside"
    outside.write_text("keep")
    (tmp_path / f".key-{stripe:02x}.lock").symlink_to(outside)
    with pytest.raises(RuntimeError, match="provider failed"), cache.lock("a"):
        raise RuntimeError("provider failed")
    assert outside.read_text() == "keep"


def test_held_singleflight_lock_has_bounded_wait_and_warning(tmp_path):
    import fcntl

    import pytest

    cache = WebCache(tmp_path, lock_timeout=0.02)
    stripe = int(hashlib.sha256(b"a").hexdigest()[:8], 16) % 64
    with (tmp_path / f".key-{stripe:02x}.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(CacheBusy), cache.lock("a"):
            raise AssertionError("must not duplicate provider request on busy lock")
        assert time.monotonic() - started < 1
        assert cache.warning == "cache_lock_timeout"


def test_miss_is_not_an_io_warning(tmp_path):
    cache = WebCache(tmp_path)
    assert cache.get("absent", 60) is None
    assert cache.warning is None
    assert cache.put("a", payload())
    entries(tmp_path)[0].write_text("corrupted")
    assert cache.get("a", 60) is None
    assert cache.warning == "cache_corrupt"


def test_reduced_host_budget_is_enforced_on_reopen(tmp_path):
    cache = WebCache(tmp_path)
    assert cache.put("a", payload("x" * 1000))
    smaller = WebCache(tmp_path, max_bytes=100)
    assert sum(p.stat().st_size for p in tmp_path.iterdir()) <= 100
    assert smaller.get("a", 60) is None


def test_symlink_root_is_disabled_without_chmod_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    cache = WebCache(link)
    assert cache.warning == "cache_unavailable"
    assert not cache.put("a", payload())
    assert target.stat().st_mode & 0o777 == 0o755


def test_transient_lock_creation_missing_is_retried_with_security_flags(tmp_path, monkeypatch):
    import errno
    import os

    original_open = os.open
    attempts = []

    def transient_open(name, flags, mode=0o777, *, dir_fd=None):
        if name == '.cache.lock':
            attempts.append((flags, dir_fd))
            if len(attempts) < 4:
                raise FileNotFoundError(errno.ENOENT, 'transient concurrent creation', name)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr('runtime.local.web_cache.os.open', transient_open)
    cache = WebCache(tmp_path)
    assert cache.warning is None
    assert len(attempts) == 4
    assert all(flags & os.O_CREAT and flags & os.O_NOFOLLOW for flags, _ in attempts)
    assert all(directory == cache._directory for _, directory in attempts)
    assert cache.put('key', payload())
    assert cache.get('key', 60) == payload()


def test_singleflight_transient_missing_does_not_run_provider_unlocked(tmp_path, monkeypatch):
    import errno
    import os

    cache = WebCache(tmp_path)
    original_open = os.open
    attempts = []

    def transient_open(name, flags, mode=0o777, *, dir_fd=None):
        if isinstance(name, str) and name.startswith('.key-'):
            attempts.append(name)
            if len(attempts) < 3:
                raise FileNotFoundError(errno.ENOENT, 'transient concurrent creation', name)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr('runtime.local.web_cache.os.open', transient_open)
    calls = []
    for _ in range(2):
        with cache.lock('same'):
            if cache.get('same', 60) is None:
                calls.append(1)
                assert cache.put('same', payload())
    assert len(calls) == 1
    assert len(attempts) == 4
    assert cache.warning is None


def test_lock_creation_missing_retry_is_bounded(tmp_path, monkeypatch):
    import errno
    import os

    original_open = os.open
    attempts = []

    def missing_open(name, flags, mode=0o777, *, dir_fd=None):
        if name == '.cache.lock':
            attempts.append(name)
            raise FileNotFoundError(errno.ENOENT, 'persistent missing', name)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr('runtime.local.web_cache.os.open', missing_open)
    cache = WebCache(tmp_path)
    assert len(attempts) == 4
    assert cache._directory is None
    assert cache.warning == 'cache_unavailable'


def test_other_open_errors_are_not_retried(tmp_path, monkeypatch):
    import errno
    import os

    original_open = os.open
    attempts = []

    def denied_open(name, flags, mode=0o777, *, dir_fd=None):
        if name == '.cache.lock':
            attempts.append(name)
            raise PermissionError(errno.EACCES, 'denied', name)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr('runtime.local.web_cache.os.open', denied_open)
    cache = WebCache(tmp_path)
    assert len(attempts) == 1
    assert cache._directory is None
    assert cache.warning == 'cache_unavailable'


def test_removed_directory_does_not_get_recreated_by_lock_retry(tmp_path, monkeypatch):
    import os

    cache = WebCache(tmp_path / 'cache')
    (cache.root / '.cache.lock').unlink()
    cache.root.rmdir()
    original_open = os.open
    attempts = []

    def counted_open(name, flags, mode=0o777, *, dir_fd=None):
        attempts.append(name)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr('runtime.local.web_cache.os.open', counted_open)
    with cache.lock('key'):
        pass
    assert len(attempts) == 1
    assert not cache.root.exists()
    assert cache.warning == 'cache_unavailable'


def test_symlink_after_transient_missing_is_not_followed_or_retried(tmp_path, monkeypatch):
    import errno
    import os

    cache = WebCache(tmp_path / 'cache')
    outside = tmp_path / 'outside'
    outside.write_text('untouched')
    outside.chmod(0o644)
    original_open = os.open
    attempts = []

    def replaced_open(name, flags, mode=0o777, *, dir_fd=None):
        if isinstance(name, str) and name.startswith('.key-'):
            attempts.append(name)
            if len(attempts) == 1:
                (cache.root / name).symlink_to(outside)
                raise FileNotFoundError(errno.ENOENT, 'transient concurrent creation', name)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr('runtime.local.web_cache.os.open', replaced_open)
    with cache.lock('key'):
        pass
    assert len(attempts) == 2
    assert outside.read_text() == 'untouched'
    assert outside.stat().st_mode & 0o777 == 0o644
    assert cache.warning == 'cache_unavailable'


def test_payload_open_missing_is_not_retried(tmp_path, monkeypatch):
    import errno
    import os

    cache = WebCache(tmp_path)
    original_open = os.open
    attempts = []

    def missing_payload(name, flags, mode=0o777, *, dir_fd=None):
        if name.endswith('.json') or name.startswith('.pending-'):
            attempts.append(name)
            raise FileNotFoundError(errno.ENOENT, 'missing payload', name)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr('runtime.local.web_cache.os.open', missing_payload)
    assert cache.get('key', 60) is None
    assert cache.put('key', payload()) is False
    assert len(attempts) == 2


def test_removed_root_with_stale_link_count_is_not_retried(tmp_path, monkeypatch):
    import os

    cache = WebCache(tmp_path / 'cache')
    (cache.root / '.cache.lock').unlink()
    cache.root.rmdir()
    original_open, original_fstat = os.open, os.fstat
    attempts = []

    def stale_link_count(descriptor):
        result = original_fstat(descriptor)
        if descriptor == cache._directory:
            fields = list(result)
            fields[3] = 2  # macOS can retain this count on a removed directory fd.
            return os.stat_result(fields)
        return result

    def counted_open(name, flags, mode=0o777, *, dir_fd=None):
        attempts.append(name)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr('runtime.local.web_cache.os.fstat', stale_link_count)
    monkeypatch.setattr('runtime.local.web_cache.os.open', counted_open)
    with cache.lock('key'):
        pass
    assert len(attempts) == 1
    assert not cache.root.exists()
    assert cache.warning == 'cache_unavailable'


def test_root_replacement_after_missing_does_not_redirect_retry(tmp_path, monkeypatch):
    import errno
    import os

    cache = WebCache(tmp_path / 'cache')
    original_open = os.open
    moved = tmp_path / 'original-cache'
    attempts = []

    def replaced_root(name, flags, mode=0o777, *, dir_fd=None):
        if isinstance(name, str) and name.startswith('.key-'):
            attempts.append(name)
            if len(attempts) == 1:
                cache.root.rename(moved)
                cache.root.mkdir(mode=0o755)
                raise FileNotFoundError(errno.ENOENT, 'transient creation', name)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr('runtime.local.web_cache.os.open', replaced_root)
    with cache.lock('key'):
        pass
    assert len(attempts) == 1
    assert list(cache.root.iterdir()) == []
    assert list(moved.glob('.key-*')) == []
    assert cache.root.stat().st_mode & 0o777 == 0o755
    assert cache.warning == 'cache_unavailable'


def test_root_symlink_to_same_inode_after_missing_is_not_retried(tmp_path, monkeypatch):
    import errno
    import os

    cache = WebCache(tmp_path / 'cache')
    original_open = os.open
    moved = tmp_path / 'original-cache'
    attempts = []

    def symlinked_root(name, flags, mode=0o777, *, dir_fd=None):
        if isinstance(name, str) and name.startswith('.key-'):
            attempts.append(name)
            if len(attempts) == 1:
                cache.root.rename(moved)
                cache.root.symlink_to(moved, target_is_directory=True)
                raise FileNotFoundError(errno.ENOENT, 'transient creation', name)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr('runtime.local.web_cache.os.open', symlinked_root)
    with cache.lock('key'):
        pass
    assert len(attempts) == 1
    assert cache.root.is_symlink()
    assert list(moved.glob('.key-*')) == []
    assert cache.warning == 'cache_unavailable'
