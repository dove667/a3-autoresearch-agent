from remote_test_support import patch_controller

"""Memory sharing admits small jobs without exceeding the device pool."""

import json
from argparse import Namespace

import pytest

from runtime.remote import remote_jobs as runtime


def settings(**kwargs):
    return Namespace(max_gpus=4, gpu_memory_mb=512, gpu_headroom_mb=1024, gpu_ids="", **kwargs)


def device(index, used=0, total=24000):
    return {
        "index": str(index),
        "uuid": f"GPU-{index}",
        "used_mb": used,
        "total_mb": total,
        "utilization": 0,
    }


def job(index, reservation=512):
    return {
        "job_id": f"job-{index}",
        "container_id": f"container-{index}",
        "gpu_uuid": f"GPU-{index}",
        "gpu_memory_mb": reservation,
        "gpu_shared": True,
    }


def test_busy_project_device_can_receive_another_job(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    patch_controller(monkeypatch, runtime, "gpu_devices", lambda: [device(0, used=2000)])
    patch_controller(monkeypatch, runtime, "gpu_process_memory", dict)
    patch_controller(monkeypatch, runtime, "container_gpu_memory", lambda *a: 0)
    assert runtime.select_shared_gpu(store, settings(), [job(0)], 512) == "GPU-0"


def test_pending_reservations_prevent_startup_overbooking(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    patch_controller(monkeypatch, runtime, "gpu_devices", lambda: [device(0, used=0, total=4096)])
    patch_controller(monkeypatch, runtime, "gpu_process_memory", dict)
    patch_controller(monkeypatch, runtime, "container_gpu_memory", lambda *a: 0)
    assert runtime.select_shared_gpu(store, settings(), [job(0, 3072)], 512) is None


def test_loaded_memory_is_not_counted_twice(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    patch_controller(monkeypatch, runtime, "gpu_devices", lambda: [device(0, used=17000)])
    patch_controller(monkeypatch, runtime, "gpu_process_memory", dict)
    patch_controller(monkeypatch, runtime, "container_gpu_memory", lambda *a: 16000)
    assert runtime.select_shared_gpu(store, settings(), [job(0, 16000)], 5000) == "GPU-0"


def test_fifth_device_is_not_added_when_four_are_active(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    patch_controller(
        monkeypatch,
        runtime,
        "gpu_devices",
        lambda: [device(i, used=23500 if i < 4 else 0) for i in range(5)],
    )
    patch_controller(monkeypatch, runtime, "gpu_process_memory", dict)
    patch_controller(monkeypatch, runtime, "container_gpu_memory", lambda *a: 0)
    assert runtime.select_shared_gpu(store, settings(), [job(i) for i in range(4)], 512) is None


def test_device_with_most_available_memory_is_selected(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    patch_controller(monkeypatch, runtime, "gpu_devices", lambda: [device(0, used=2000), device(1)])
    patch_controller(monkeypatch, runtime, "gpu_process_memory", dict)
    assert runtime.select_shared_gpu(store, settings(), [], 512) == "GPU-1"


def test_twenty_gib_reservation_cap_and_four_gib_buffer(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    patch_controller(
        monkeypatch, runtime, "gpu_devices", lambda: [device(0, used=1000, total=24576)]
    )
    patch_controller(monkeypatch, runtime, "gpu_process_memory", dict)
    patch_controller(monkeypatch, runtime, "container_gpu_memory", lambda *a: 1000)
    limits = settings()
    limits.gpu_capacity_mb = 20480
    limits.gpu_headroom_mb = 4096
    assert runtime.select_shared_gpu(store, limits, [job(0, 20000)], 480) == "GPU-0"
    assert runtime.select_shared_gpu(store, limits, [job(0, 20000)], 481) is None


def test_two_gib_buffer_and_project_reservation_cap_are_independent(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    patch_controller(
        monkeypatch, runtime, "gpu_devices", lambda: [device(0, used=4096, total=24576)]
    )
    patch_controller(monkeypatch, runtime, "gpu_process_memory", dict)
    limits = settings()
    limits.gpu_capacity_mb = 20480
    limits.gpu_headroom_mb = 2048
    report = {}
    assert runtime.select_shared_gpu(store, limits, [], 18432, diagnostics=report) == "GPU-0"
    assert report["devices"][0]["available_mb"] == 18432
    assert runtime.select_shared_gpu(store, limits, [], 18433) is None


def test_more_than_four_jobs_can_share_one_device(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    patch_controller(monkeypatch, runtime, "gpu_devices", lambda: [device(0, used=2000)])
    patch_controller(monkeypatch, runtime, "gpu_process_memory", dict)
    patch_controller(monkeypatch, runtime, "container_gpu_memory", lambda *a: 100)
    existing = [
        {**job(0), "job_id": f"job-{i}", "container_id": f"container-{i}"} for i in range(6)
    ]
    assert runtime.select_shared_gpu(store, settings(), existing, 512) == "GPU-0"


def test_observed_memory_accounts_for_external_load(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    patch_controller(monkeypatch, runtime, "gpu_devices", lambda: [device(0, used=22000)])
    patch_controller(monkeypatch, runtime, "gpu_process_memory", dict)
    patch_controller(monkeypatch, runtime, "container_gpu_memory", lambda *a: 512)
    assert runtime.select_shared_gpu(store, settings(), [job(0)], 2048) is None


def test_unlimited_job_count_does_not_reject_small_jobs(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)

    def reject_count(*args):
        raise AssertionError("job-count cap reached")

    patch_controller(monkeypatch, runtime, "count_active_jobs", reject_count)
    patch_controller(monkeypatch, runtime, "create_container", lambda *a: "new-container")
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **kw: None)
    args = settings(max_jobs=0, timeout=0, share_gpus=True, run_storage_bytes=0)
    result = runtime.submit(
        store, {"experiment_id": "small", "command": ["true"], "gpu": False}, args
    )
    assert result["status"] == "created"


def test_idle_device_report_explains_admitted_capacity(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    patch_controller(monkeypatch, runtime, "gpu_devices", lambda: [device(1, used=1, total=24564)])
    patch_controller(monkeypatch, runtime, "gpu_process_memory", dict)
    limits = settings()
    limits.gpu_headroom_mb = 4096
    limits.gpu_capacity_mb = 20480
    report = {}

    selected = runtime.select_shared_gpu(store, limits, [], 14000, diagnostics=report)

    assert selected == "GPU-1"
    assert report["selected_gpu_uuid"] == selected
    assert report["requested_memory_mb"] == 14000
    assert report["devices"][0]["available_mb"] == 20467
    assert report["devices"][0]["reason"] == "selected"


def test_external_load_is_admitted_when_memory_fits_even_if_computing(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    occupied = {**device(6, used=4817), "utilization": 95}
    patch_controller(monkeypatch, runtime, "gpu_devices", lambda: [occupied])
    patch_controller(monkeypatch, runtime, "gpu_process_memory", dict)
    report = {}

    assert runtime.select_shared_gpu(store, settings(), [], 8192, diagnostics=report) == "GPU-6"
    assert report["devices"][0]["reason"] == "selected"
    assert report["devices"][0]["used_mb"] == 4817
    assert report["devices"][0]["available_mb"] == 18159


def test_memory_shortage_report_includes_pending_reservations(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    patch_controller(monkeypatch, runtime, "gpu_devices", lambda: [device(0, total=4096)])
    patch_controller(monkeypatch, runtime, "gpu_process_memory", dict)
    patch_controller(monkeypatch, runtime, "container_gpu_memory", lambda *args: 0)
    report = {}

    selected = runtime.select_shared_gpu(store, settings(), [job(0, 3072)], 512, diagnostics=report)

    assert selected is None
    assert report["devices"][0]["reason"] == "insufficient_memory"
    assert report["devices"][0]["pending_mb"] == 3072
    assert report["devices"][0]["available_mb"] == 0


def test_busy_submission_returns_and_preserves_gpu_report(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    patch_controller(monkeypatch, runtime, "gpu_devices", lambda: [device(0, used=22500)])
    patch_controller(monkeypatch, runtime, "gpu_process_memory", dict)
    limits = settings(max_jobs=0, timeout=0, share_gpus=True, run_storage_bytes=0)

    result = runtime.submit(
        store,
        {"experiment_id": "debug-owner", "command": ["true"], "gpu": True},
        limits,
    )

    assert result["status"] == "resource_busy"
    assert result["reason"] == "no_eligible_gpu"
    assert result["gpu_allocation"]["devices"][0]["reason"] == "insufficient_memory"
    events = [
        json.loads(line) for line in (tmp_path / "scheduler-events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["result"]["gpu_allocation"] == result["gpu_allocation"]
    assert events[-1]["experiment_id"] == "debug-owner"
    assert not list((tmp_path / "jobs").iterdir())


def test_selected_gpu_report_is_saved_without_uploaded_content(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    patch_controller(monkeypatch, runtime, "gpu_devices", lambda: [device(1, used=1)])
    patch_controller(monkeypatch, runtime, "gpu_process_memory", dict)
    patch_controller(monkeypatch, runtime, "create_container", lambda *args: "test-container")
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *args, **kwargs: None)
    limits = settings(max_jobs=0, timeout=0, share_gpus=True, run_storage_bytes=0)
    request = {
        "experiment_id": "debug-owner",
        "command": ["python", "private.py"],
        "files": {"private.py": "private uploaded content"},
        "gpu": True,
    }

    result = runtime.submit(store, request, limits)

    saved = store.load(result["job_id"], "debug-owner")
    assert result["status"] == "created"
    assert saved["gpu_allocation"] == result["gpu_allocation"]
    log = (tmp_path / "scheduler-events.jsonl").read_text()
    assert json.loads(log)["result"]["gpu_allocation"] == saved["gpu_allocation"]
    assert "private.py" not in log
    assert "private uploaded content" not in log


@pytest.mark.parametrize("shared", [False, True])
def test_diagnostic_write_failure_prevents_launch_and_releases_lease(tmp_path, monkeypatch, shared):
    store = runtime.JobStore(tmp_path)
    patch_controller(monkeypatch, runtime, "gpu_devices", lambda: [device(1)])
    patch_controller(monkeypatch, runtime, "gpu_process_memory", dict)
    patch_controller(
        monkeypatch,
        runtime,
        "acquire_gpu",
        lambda current, allowed: (
            "GPU-1",
            runtime.GpuLease.try_acquire(current.root / "locks", "GPU-1"),
        ),
    )

    def fail_log(*args):
        raise OSError("diagnostic storage unavailable")

    def forbidden_launch(*args, **kwargs):
        pytest.fail("diagnostic failure must prevent container and watcher creation")

    patch_controller(monkeypatch, runtime, "record_scheduler_event", fail_log)
    patch_controller(monkeypatch, runtime, "create_container", forbidden_launch)
    monkeypatch.setattr(runtime.subprocess, "Popen", forbidden_launch)
    limits = settings(max_jobs=0, timeout=0, share_gpus=shared, run_storage_bytes=0)

    with pytest.raises(OSError, match="diagnostic storage unavailable"):
        runtime.submit(
            store,
            {"experiment_id": "debug-owner", "command": ["true"], "gpu": True},
            limits,
        )

    assert not list((tmp_path / "jobs").iterdir())
    lease = runtime.GpuLease.try_acquire(tmp_path / "locks", "GPU-1")
    assert lease is not None
    lease.close()
