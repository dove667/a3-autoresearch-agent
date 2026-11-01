"""Memory sharing admits small jobs without exceeding the device pool."""

from argparse import Namespace

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
    monkeypatch.setattr(runtime, "gpu_devices", lambda: [device(0, used=2000)])
    monkeypatch.setattr(runtime, "gpu_process_memory", dict)
    monkeypatch.setattr(runtime, "container_gpu_memory", lambda *a: 0)
    assert runtime.select_shared_gpu(store, settings(), [job(0)], 512) == "GPU-0"


def test_pending_reservations_prevent_startup_overbooking(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    monkeypatch.setattr(runtime, "gpu_devices", lambda: [device(0, used=0, total=4096)])
    monkeypatch.setattr(runtime, "gpu_process_memory", dict)
    monkeypatch.setattr(runtime, "container_gpu_memory", lambda *a: 0)
    assert runtime.select_shared_gpu(store, settings(), [job(0, 3072)], 512) is None


def test_loaded_memory_is_not_counted_twice(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    monkeypatch.setattr(runtime, "gpu_devices", lambda: [device(0, used=17000)])
    monkeypatch.setattr(runtime, "gpu_process_memory", dict)
    monkeypatch.setattr(runtime, "container_gpu_memory", lambda *a: 16000)
    assert runtime.select_shared_gpu(store, settings(), [job(0, 16000)], 5000) == "GPU-0"


def test_fifth_device_is_not_added_when_four_are_active(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    monkeypatch.setattr(
        runtime, "gpu_devices", lambda: [device(i, used=23500 if i < 4 else 0) for i in range(5)]
    )
    monkeypatch.setattr(runtime, "gpu_process_memory", dict)
    monkeypatch.setattr(runtime, "container_gpu_memory", lambda *a: 0)
    assert runtime.select_shared_gpu(store, settings(), [job(i) for i in range(4)], 512) is None


def test_busy_external_device_is_not_adopted(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    monkeypatch.setattr(runtime, "gpu_devices", lambda: [device(0, used=2000), device(1)])
    monkeypatch.setattr(runtime, "gpu_process_memory", dict)
    assert runtime.select_shared_gpu(store, settings(), [], 512) == "GPU-1"


def test_twenty_gib_reservation_cap_and_four_gib_buffer(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    monkeypatch.setattr(runtime, "gpu_devices", lambda: [device(0, used=1000, total=24576)])
    monkeypatch.setattr(runtime, "gpu_process_memory", dict)
    monkeypatch.setattr(runtime, "container_gpu_memory", lambda *a: 1000)
    limits = settings()
    limits.gpu_capacity_mb = 20480
    limits.gpu_headroom_mb = 4096
    assert runtime.select_shared_gpu(store, limits, [job(0, 20000)], 480) == "GPU-0"
    assert runtime.select_shared_gpu(store, limits, [job(0, 20000)], 481) is None


def test_more_than_four_jobs_can_share_one_device(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    monkeypatch.setattr(runtime, "gpu_devices", lambda: [device(0, used=2000)])
    monkeypatch.setattr(runtime, "gpu_process_memory", dict)
    monkeypatch.setattr(runtime, "container_gpu_memory", lambda *a: 100)
    existing = [
        {**job(0), "job_id": f"job-{i}", "container_id": f"container-{i}"} for i in range(6)
    ]
    assert runtime.select_shared_gpu(store, settings(), existing, 512) == "GPU-0"


def test_observed_memory_accounts_for_external_load(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)
    monkeypatch.setattr(runtime, "gpu_devices", lambda: [device(0, used=22000)])
    monkeypatch.setattr(runtime, "gpu_process_memory", dict)
    monkeypatch.setattr(runtime, "container_gpu_memory", lambda *a: 512)
    assert runtime.select_shared_gpu(store, settings(), [job(0)], 2048) is None


def test_unlimited_job_count_does_not_reject_small_jobs(tmp_path, monkeypatch):
    store = runtime.JobStore(tmp_path)

    def reject_count(*args):
        raise AssertionError("job-count cap reached")

    monkeypatch.setattr(runtime, "count_active_jobs", reject_count)
    monkeypatch.setattr(runtime, "create_container", lambda *a: "new-container")
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **kw: None)
    args = settings(max_jobs=0, timeout=0, share_gpus=True, run_storage_bytes=0)
    result = runtime.submit(
        store, {"experiment_id": "small", "command": ["true"], "gpu": False}, args
    )
    assert result["status"] == "created"
