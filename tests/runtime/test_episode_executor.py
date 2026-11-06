import json
import os
import signal
import subprocess
import sys
import time

import pytest

from a3_research_agent.execution import episode_executor as executor


class Monitor:
    cpus = 8

    def sample(self):
        return {
            "time": 0,
            "cpu_cores": 0.2,
            "cpu_limit": 8,
            "memory_bytes": 10,
            "memory_limit": 10000,
            "pids": 3,
            "pids_limit": 1024,
            "new_oom": False,
            "gpu_available": True,
        }


def test_managed_batch_waits_through_resource_pressure_without_returning_to_model(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("A3_JOB_MANAGED", "1")
    path = manifest(tmp_path, 1, idle_timeout_seconds=1)
    start = time.monotonic()

    class TemporarilyBusy(Monitor):
        def sample(self):
            result = super().sample()
            if time.monotonic() - start < 1.3:
                result["memory_bytes"] = result["memory_limit"]
            return result

    assert executor.EpisodeExecutor(path, tmp_path / "batch", TemporarilyBusy()).run() == 0
    assert time.monotonic() - start > 1


def manifest(tmp_path, count=6, **options):
    items = []
    for i in range(count):
        report = tmp_path / f"report-{i}.json"
        code = (
            f'import time;time.sleep(.15);open({str(report)!r},"w").write(\'{{"success":false}}\')'
        )
        items.append(
            {"id": f"episode-{i}", "command": [sys.executable, "-c", code], "report": str(report)}
        )
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            dict(version=1, episodes=items, warmup_seconds=0.05, adjust_seconds=0.05, **options)
        )
    )
    return path


def test_adaptive_execution_preserves_failed_scientific_results_and_reuses(tmp_path):
    path = manifest(tmp_path)
    batch = tmp_path / "batch"
    assert executor.EpisodeExecutor(path, batch, Monitor()).run() == 0
    summary = json.loads((batch / "summary.json").read_text())
    assert summary["complete"] == 6
    assert summary["max_parallel_observed"] > 2
    assert executor.EpisodeExecutor(path, batch, Monitor()).run() == 0
    summary = json.loads((batch / "summary.json").read_text())
    assert summary["reused_complete"] == 6
    assert summary["max_parallel_observed"] == 0
    (tmp_path / "report-0.json").write_text("{}")
    with pytest.raises(ValueError, match="report changed"):
        executor.EpisodeExecutor(path, batch, Monitor()).run()


def test_unresolved_and_existing_outputs_never_replayed(tmp_path):
    path = manifest(tmp_path, 1)
    batch = tmp_path / "batch"
    run = executor.EpisodeExecutor(path, batch, Monitor())
    run.prepare()
    run.rows["episode-0"]["status"] = "running"
    run.save()
    with pytest.raises(ValueError, match="unresolved"):
        executor.EpisodeExecutor(path, batch, Monitor()).run()
    (tmp_path / "report-0.json").write_text("{}")
    with pytest.raises(ValueError, match="Untracked"):
        executor.EpisodeExecutor(path, tmp_path / "other", Monitor()).run()


def test_quota_pressure_and_missing_measurement():
    sample = Monitor().sample()
    assert executor.choose_parallelism(2, 8, sample, 2, 10, 0)[0] == 3
    sample["cpu_cores"] = 7.5
    assert executor.choose_parallelism(2, 8, sample, 2, 10, 0)[0] == 2
    sample["memory_bytes"] = 9000
    assert executor.choose_parallelism(4, 8, sample, 4, 10, 0)[0] == 3
    sample["memory_bytes"] = 10
    sample["gpu_available"] = False
    assert executor.choose_parallelism(2, 8, sample, 2, 10, 0)[0] == 1
    sample.update(
        gpu_limit_mb=100,
        gpu_budget_used_mb=85,
        gpu_used_mb=100,
        gpu_total_mb=1000,
        gpu_available=True,
    )
    assert executor.choose_parallelism(4, 8, sample, 4, 10, 0)[0] == 3


def test_cgroup_cpu_uses_counter_delta_and_entire_memory(tmp_path, monkeypatch):
    for name, content in {
        "cpu.max": "400000 100000",
        "memory.max": "10000000",
        "memory.current": "123456",
        "pids.max": "128",
        "pids.current": "5",
        "memory.events": "oom_kill 0",
        "cpu.stat": "usage_usec 1000000",
    }.items():
        (tmp_path / name).write_text(content)
    monkeypatch.delenv("A3_JOB_GPU_MEMORY_MB", raising=False)
    times = iter([10, 12])
    monkeypatch.setattr(executor.time, "monotonic", lambda: next(times))
    monitor = executor.ResourceMonitor(tmp_path)
    assert monitor.sample()["cpu_cores"] is None
    (tmp_path / "cpu.stat").write_text("usage_usec 7000000")
    sample = monitor.sample()
    assert sample["cpu_cores"] == 3
    assert sample["memory_bytes"] == 123456
    assert monitor.cpus == 4


def test_failed_process_or_invalid_report_is_not_counted_complete(tmp_path):
    path = manifest(tmp_path, 1)
    data = json.loads(path.read_text())
    data["episodes"][0]["command"] = [sys.executable, "-c", "pass"]
    path.write_text(json.dumps(data))
    assert executor.EpisodeExecutor(path, tmp_path / "batch", Monitor()).run() == 1
    assert json.loads((tmp_path / "batch/summary.json").read_text())["complete"] == 0


def test_manifest_rejects_duplicate_reports_and_invalid_options(tmp_path):
    path = manifest(tmp_path, 2)
    data = json.loads(path.read_text())
    data["episodes"][1]["report"] = data["episodes"][0]["report"]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="distinct"):
        executor.load_manifest(path)
    path = manifest(tmp_path, 1, threads_per_episode=0)
    with pytest.raises(ValueError, match="threads_per_episode"):
        executor.load_manifest(path)


def test_admission_stops_immediately_under_pressure():
    sample = Monitor().sample()
    assert executor.admission_allowed(sample, 0, 0, 0)
    sample["memory_bytes"] = 9000
    assert not executor.admission_allowed(sample, 0, 0, 0)
    sample["memory_bytes"] = 10
    sample["gpu_available"] = False
    assert not executor.admission_allowed(sample, 1, 0, 0)


@pytest.mark.parametrize(
    "name",
    [
        "ledger.json",
        "ledger.tmp",
        "summary.json",
        "metrics.jsonl",
        "batch.lock",
        "episode-0.stdout",
        "episode-0.stderr",
    ],
)
def test_reports_cannot_overwrite_control_files(tmp_path, name):
    path = manifest(tmp_path, 1)
    data = json.loads(path.read_text())
    data["episodes"][0]["report"] = str(tmp_path / "batch" / name)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="control files"):
        executor.EpisodeExecutor(path, tmp_path / "batch", Monitor())
    assert not (tmp_path / "batch").exists()


def test_background_descendant_is_cleaned_and_not_complete(tmp_path):
    path = manifest(tmp_path, 1)
    data = json.loads(path.read_text())
    report = data["episodes"][0]["report"]
    code = (
        "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        f"open({report!r},'w').write('{{}}')"
    )
    data["episodes"][0]["command"] = [sys.executable, "-c", code]
    path.write_text(json.dumps(data))
    assert executor.EpisodeExecutor(path, tmp_path / "batch", Monitor()).run() == 1
    row = json.loads((tmp_path / "batch/ledger.json").read_text())["episodes"]["episode-0"]
    assert "descendants" in row["error"]


def test_resource_blocked_batch_exits_with_pending_and_can_resume(tmp_path, monkeypatch):
    path = manifest(tmp_path, 1, idle_timeout_seconds=1)
    batch = tmp_path / "batch"

    class PressuredMonitor(Monitor):
        def sample(self):
            return dict(super().sample(), memory_bytes=9000)

    elapsed = [100.0]
    with monkeypatch.context() as patch:
        patch.setattr(executor.time, "monotonic", lambda: elapsed[0])
        patch.setattr(
            executor.time, "sleep", lambda duration: elapsed.__setitem__(0, elapsed[0] + duration)
        )
        assert executor.EpisodeExecutor(path, batch, PressuredMonitor()).run() == 75
    summary = json.loads((batch / "summary.json").read_text())
    assert summary["resource_blocked"] and summary["max_parallel_observed"] == 0
    row = json.loads((batch / "ledger.json").read_text())["episodes"]["episode-0"]
    assert row == {"status": "pending"}
    assert not (tmp_path / "report-0.json").exists()
    assert executor.EpisodeExecutor(path, batch, Monitor()).run() == 0


def test_pending_report_that_appears_between_runs_is_never_overwritten(tmp_path):
    path = manifest(tmp_path, 1)
    batch = tmp_path / "batch"
    executor.EpisodeExecutor(path, batch, Monitor()).prepare()
    report = tmp_path / "report-0.json"
    report.write_text('{"existing_scientific_result": true}')
    original = report.read_bytes()
    with pytest.raises(ValueError, match="report"):
        executor.EpisodeExecutor(path, batch, Monitor()).run()
    assert report.read_bytes() == original


def test_report_appearing_after_prepare_is_preserved_without_launch(tmp_path):
    path = manifest(tmp_path, 1)
    run = executor.EpisodeExecutor(path, tmp_path / "batch", Monitor())
    run.prepare()
    report = tmp_path / "report-0.json"
    report.write_text('{"existing_scientific_result": true}')
    original = report.read_bytes()
    run.launch(run.manifest["episodes"][0])
    assert run.rows["episode-0"]["status"] == "failed"
    assert not run.active
    assert report.read_bytes() == original


def test_measurement_loss_stops_new_admission_until_recovery(tmp_path):
    path = manifest(tmp_path, 8)

    class IntermittentMonitor(Monitor):
        def __init__(self):
            self.calls = 0
            self.available = True

        def sample(self):
            self.calls += 1
            self.available = not 15 <= self.calls <= 35
            return dict(super().sample(), gpu_available=self.available)

    monitor = IntermittentMonitor()
    run = executor.EpisodeExecutor(path, tmp_path / "batch", monitor)
    launch = run.launch
    admitted_during_loss = []

    def checked_launch(item):
        if not monitor.available:
            admitted_during_loss.append(item["id"])
        launch(item)

    run.launch = checked_launch
    assert run.run() == 0
    assert monitor.calls > 35
    assert not admitted_during_loss
    metrics = [
        json.loads(line) for line in (tmp_path / "batch/metrics.jsonl").read_text().splitlines()
    ]
    assert any(not row["gpu_available"] and row["target"] == 1 for row in metrics)


@pytest.mark.parametrize("fault", [None, "stale", "error", "nonfinite", "missing"])
def test_host_gpu_telemetry_is_validated_before_admission(tmp_path, monkeypatch, fault):
    for name, content in {
        "cpu.max": "400000 100000",
        "memory.max": "10000000",
        "memory.current": "123456",
        "pids.max": "128",
        "pids.current": "5",
        "memory.events": "oom_kill 0",
        "cpu.stat": "usage_usec 1000000",
    }.items():
        (tmp_path / name).write_text(content)
    telemetry = {
        "sampled_at": time.time(),
        "gpu_used_mb": 1000,
        "gpu_total_mb": 24000,
        "gpu_util_percent": 10,
        "gpu_budget_used_mb": 100,
    }
    if fault == "stale":
        telemetry["sampled_at"] -= 11
    elif fault == "error":
        telemetry["error"] = "unavailable"
    elif fault == "nonfinite":
        telemetry["gpu_budget_used_mb"] = float("nan")
    elif fault == "missing":
        del telemetry["gpu_total_mb"]
    original_read_text = executor.Path.read_text

    def read_text(path, *args, **kwargs):
        if str(path) == "/run/a3/telemetry/resources.json":
            return json.dumps(telemetry)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(executor.Path, "read_text", read_text)
    monkeypatch.setenv("A3_JOB_GPU_MEMORY_MB", "512")
    sample = executor.ResourceMonitor(tmp_path).sample()
    assert sample["gpu_available"] is (fault is None)
    assert executor.admission_allowed(sample, 0, 0, 0) is (fault is None)


def test_sigterm_preserves_completed_report_and_cancels_only_inflight(tmp_path):
    path = manifest(tmp_path, 3, max_parallel=1)
    data = json.loads(path.read_text())
    pidfile = tmp_path / "active.pid"
    data["episodes"][1]["command"] = [
        sys.executable,
        "-c",
        (f"import os,time;open({str(pidfile)!r},'w').write(str(os.getpid()));time.sleep(30)"),
    ]
    path.write_text(json.dumps(data))
    batch = tmp_path / "batch"
    monitor_sample = repr(Monitor().sample())
    script = (
        "from pathlib import Path\n"
        "from a3_research_agent.execution.episode_executor import EpisodeExecutor\n"
        "class Monitor:\n"
        " cpus=8\n"
        f" def sample(self): return {monitor_sample}\n"
        f"raise SystemExit(EpisodeExecutor(Path({str(path)!r}),Path({str(batch)!r}),Monitor()).run())"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(process.communicate()[1].decode())
            time.sleep(0.01)
        assert pidfile.exists()
        first_report = (tmp_path / "report-0.json").read_bytes()
        os.kill(process.pid, signal.SIGTERM)
        assert process.wait(timeout=5) == 130
        rows = json.loads((batch / "ledger.json").read_text())["episodes"]
        assert rows["episode-0"]["status"] == "complete"
        assert rows["episode-1"]["status"] == "failed"
        assert rows["episode-2"]["status"] == "pending"
        assert (tmp_path / "report-0.json").read_bytes() == first_report
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
