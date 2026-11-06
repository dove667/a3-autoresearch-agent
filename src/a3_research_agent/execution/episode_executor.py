"""Run independent episode commands with durable results and quota-aware concurrency.

This stdlib-only module is also mounted as a standalone script in remote jobs.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import time
from pathlib import Path


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_number(path):
    value = path.read_text().strip()
    return None if value == "max" else int(value)


class ResourceMonitor:
    """Measure the whole cgroup using counters, including all episode descendants."""

    def __init__(self, root=Path("/sys/fs/cgroup")):
        self.root = root
        quota, period = (root / "cpu.max").read_text().split()
        self.cpus = float(os.cpu_count() or 1) if quota == "max" else int(quota) / int(period)
        self.memory_limit = read_number(root / "memory.max")
        if self.memory_limit is None:
            raise ValueError("A finite cgroup memory limit is required")
        self.pids_limit = read_number(root / "pids.max")
        self.gpu_limit_mb = int(os.environ.get("A3_JOB_GPU_MEMORY_MB", "0"))
        self.gpu_uuid = os.environ.get("A3_JOB_GPU_UUID", "")
        self.previous = None
        self.last_oom = None

    def sample(self):
        now = time.monotonic()
        stats = dict(line.split() for line in (self.root / "cpu.stat").read_text().splitlines())
        usage = int(stats["usage_usec"]) / 1e6
        cpu = None
        if self.previous:
            cpu = max(0, (usage - self.previous[1]) / (now - self.previous[0]))
        self.previous = (now, usage)
        events = dict(
            line.split() for line in (self.root / "memory.events").read_text().splitlines()
        )
        oom = int(events.get("oom_kill", 0))
        result = {
            "time": now,
            "cpu_cores": cpu,
            "cpu_limit": self.cpus,
            "memory_bytes": read_number(self.root / "memory.current"),
            "memory_limit": self.memory_limit,
            "pids": read_number(self.root / "pids.current"),
            "pids_limit": self.pids_limit,
            "oom_kills": oom,
            "new_oom": self.last_oom is not None and oom > self.last_oom,
            "gpu_available": not bool(self.gpu_limit_mb),
        }
        self.last_oom = oom
        if self.gpu_limit_mb:
            try:
                telemetry = json.loads(Path("/run/a3/telemetry/resources.json").read_text())
                if telemetry.get("error") or not 0 <= time.time() - telemetry["sampled_at"] <= 10:
                    raise ValueError("Host GPU telemetry unavailable or stale")
                for name in (
                    "gpu_used_mb",
                    "gpu_total_mb",
                    "gpu_util_percent",
                    "gpu_budget_used_mb",
                ):
                    number = telemetry[name]
                    if (
                        not isinstance(number, (int, float))
                        or not math.isfinite(number)
                        or number < 0
                    ):
                        raise ValueError("Invalid Host GPU telemetry")
                    result[name] = number
                result.update(gpu_available=True, gpu_limit_mb=self.gpu_limit_mb)
            except (OSError, ValueError, KeyError, TypeError):
                result["gpu_available"] = False
        return result


def choose_parallelism(
    target,
    ceiling,
    sample,
    active,
    peak_memory_per_episode,
    peak_gpu_per_episode,
    throughput_regressed=False,
):
    """Keep headroom; reduce admission without killing an active episode."""
    memory = sample["memory_bytes"]
    memory_limit = sample["memory_limit"]
    cpu = sample["cpu_cores"]
    pids_full = sample["pids_limit"] is not None and sample["pids"] >= sample["pids_limit"] * 0.85
    pressure = (
        memory >= memory_limit * 0.85 or sample["new_oom"] or pids_full or throughput_regressed
    )
    if sample.get("gpu_limit_mb") and sample["gpu_available"]:
        pressure |= (
            sample["gpu_budget_used_mb"] >= sample["gpu_limit_mb"] * 0.85
            or sample["gpu_used_mb"] >= sample["gpu_total_mb"] * 0.9
        )
    if pressure:
        return max(1, target - 1), "pressure_or_throughput_regression"
    if cpu is None or not sample["gpu_available"]:
        return 1, "measurement_unavailable"
    if cpu >= sample["cpu_limit"] * 0.9:
        return target, "cpu_saturated"
    if active < target:
        return target, "queue_draining"
    if memory + peak_memory_per_episode > memory_limit * 0.85:
        return target, "memory_headroom"
    if (
        sample.get("gpu_limit_mb")
        and sample["gpu_available"]
        and (
            sample["gpu_budget_used_mb"] + peak_gpu_per_episode > sample["gpu_limit_mb"] * 0.85
            or sample["gpu_used_mb"] + peak_gpu_per_episode > sample["gpu_total_mb"] * 0.9
        )
    ):
        return target, "gpu_headroom"
    return min(ceiling, target + 1), "resource_headroom"


def admission_allowed(sample, active, peak_memory_per_episode, peak_gpu_per_episode):
    """Stop filling slots immediately when current observations cannot justify admission."""
    if not sample["gpu_available"] or sample["new_oom"]:
        return False
    if sample["memory_bytes"] + peak_memory_per_episode >= sample["memory_limit"] * 0.85:
        return False
    if sample["pids_limit"] and sample["pids"] >= sample["pids_limit"] * 0.85:
        return False
    if sample.get("gpu_limit_mb") and (
        sample["gpu_budget_used_mb"] + peak_gpu_per_episode >= sample["gpu_limit_mb"] * 0.85
        or sample["gpu_used_mb"] + peak_gpu_per_episode >= sample["gpu_total_mb"] * 0.9
    ):
        return False
    return (
        not active
        or sample["cpu_cores"] is None
        or sample["cpu_cores"] < sample["cpu_limit"] * 0.95
    )


def load_manifest(path):
    value = json.loads(path.read_text())
    episodes = value.get("episodes")
    if (
        value.get("version") != 1
        or not isinstance(episodes, list)
        or not 1 <= len(episodes) <= 10000
    ):
        raise ValueError("Expected version 1 and 1..10000 episodes")
    identifiers, reports = set(), set()
    for item in episodes:
        identifier, command = item.get("id"), item.get("command")
        report = item.get("report")
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", identifier)
            or identifier in identifiers
        ):
            raise ValueError("Select unique canonical episode IDs")
        if (
            not isinstance(command, list)
            or not command
            or len(command) > 256
            or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command)
        ):
            raise ValueError("Each episode requires a command argument list")
        if not isinstance(report, str) or not Path(report).is_absolute():
            raise ValueError("Each episode requires an absolute JSON report path")
        report = str(Path(report).resolve())
        if report in reports:
            raise ValueError("Episodes must have distinct report paths")
        units = item.get("work_units", 1)
        if not isinstance(units, (int, float)) or not math.isfinite(units) or units <= 0:
            raise ValueError("work_units must be finite and positive")
        identifiers.add(identifier)
        reports.add(report)
    for name, default, minimum, maximum in [
        ("max_parallel", 1024, 1, 1024),
        ("threads_per_episode", 1, 1, 1024),
        ("warmup_seconds", 15, 0.05, 3600),
        ("adjust_seconds", 15, 0.05, 3600),
        ("idle_timeout_seconds", 60, 1, 3600),
    ]:
        number = value.get(name, default)
        if (
            type(number) not in (int, float)
            or not math.isfinite(number)
            or not minimum <= number <= maximum
            or (name in {"max_parallel", "threads_per_episode"} and type(number) is not int)
        ):
            raise ValueError("Invalid " + name)
        value[name] = number
    return value


class EpisodeExecutor:
    def __init__(self, manifest_path, output, monitor=None):
        self.manifest = load_manifest(manifest_path)
        self.manifest_sha = digest(manifest_path)
        self.output = output
        reserved = {
            output / name
            for name in (
                "ledger.json",
                "ledger.tmp",
                "summary.json",
                "summary.tmp",
                "metrics.jsonl",
                "batch.lock",
            )
        }
        reserved.add(manifest_path)
        for item in self.manifest["episodes"]:
            reserved.update(output / (item["id"] + suffix) for suffix in (".stdout", ".stderr"))
        resolved_reserved = {path.resolve() for path in reserved}
        if any(
            Path(item["report"]).resolve() in resolved_reserved
            for item in self.manifest["episodes"]
        ):
            raise ValueError("Episode report conflicts with executor control files")
        output.mkdir(parents=True, exist_ok=True)
        self.monitor = monitor or ResourceMonitor()
        self.interrupted = False
        self.active = {}
        self.rows = {}
        self.maximum_observed = 0

    def save(self):
        atomic_json(
            self.output / "ledger.json",
            {
                "version": 1,
                "manifest_sha256": self.manifest_sha,
                "episodes": self.rows,
            },
        )

    def prepare(self):
        ledger = self.output / "ledger.json"
        if ledger.exists():
            old = json.loads(ledger.read_text())
            if old["manifest_sha256"] != self.manifest_sha:
                raise ValueError("Batch manifest changed; use a separate output directory")
            self.rows = old["episodes"]
        for item in self.manifest["episodes"]:
            identifier, report = item["id"], Path(item["report"])
            row = self.rows.get(identifier)
            if row:
                if row["status"] == "complete":
                    if not report.is_file() or digest(report) != row["report_sha256"]:
                        raise ValueError("Completed report changed: " + identifier)
                elif row["status"] != "pending":
                    raise ValueError("Reconcile unresolved episode before reuse: " + identifier)
                elif report.exists():
                    raise ValueError(
                        "Untracked report exists; reconcile before running: " + identifier
                    )
            else:
                if report.exists():
                    raise ValueError(
                        "Untracked report exists; reconcile before running: " + identifier
                    )
                self.rows[identifier] = {"status": "pending"}
        self.save()

    def launch(self, item):
        identifier = item["id"]
        row = self.rows[identifier]
        if Path(item["report"]).exists():
            row.update(
                status="failed", error="Report appeared before launch; retained without overwrite"
            )
            self.save()
            return
        row.update(status="launching", started_at=time.time())
        self.save()
        env = dict(os.environ)
        for name in (
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "VECLIB_MAXIMUM_THREADS",
        ):
            env[name] = str(self.manifest["threads_per_episode"])
        stdout = (self.output / (identifier + ".stdout")).open("ab")
        stderr = (self.output / (identifier + ".stderr")).open("ab")
        try:
            process = subprocess.Popen(
                item["command"], env=env, stdout=stdout, stderr=stderr, start_new_session=True
            )
        except OSError as error:
            stdout.close()
            stderr.close()
            row.update(status="failed", error=str(error))
            self.save()
            return
        row.update(status="running", pid=process.pid)
        self.active[identifier] = (process, stdout, stderr, time.monotonic(), item)
        self.maximum_observed = max(self.maximum_observed, len(self.active))
        self.save()

    def collect(self):
        for identifier, (process, stdout, stderr, started, item) in list(self.active.items()):
            code = process.poll()
            if code is None:
                continue
            orphaned = False
            try:
                os.killpg(process.pid, signal.SIGTERM)
                orphaned = True
                time.sleep(0.1)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
            stdout.close()
            stderr.close()
            row = self.rows[identifier]
            row.update(
                exit_code=code,
                wall_seconds=time.monotonic() - started,
                finished_at=time.time(),
                status="failed",
            )
            report = Path(item["report"])
            try:
                # Native success=false is still a valid scientific observation.
                if (
                    code == 0
                    and not orphaned
                    and report.is_file()
                    and isinstance(json.loads(report.read_text()), dict)
                ):
                    row.update(
                        status="complete",
                        report_sha256=digest(report),
                        work_units=item.get("work_units", 1),
                    )
            except (OSError, ValueError):
                pass
            if orphaned:
                row["error"] = "Episode leader exited with background descendants; group cleaned"
            del self.active[identifier]
            self.save()

    def shutdown_children(self):
        for process, *_ in self.active.values():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 10
        while self.active and time.monotonic() < deadline:
            self.collect()
            time.sleep(0.1)
        for process, *_ in self.active.values():
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            except ProcessLookupError:
                pass
        self.collect()

    def run(self):
        with (self.output / "batch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.prepare()
            handlers = {}
            for sig in (signal.SIGINT, signal.SIGTERM):
                handlers[sig] = signal.signal(sig, lambda *_: setattr(self, "interrupted", True))
            try:
                return self.schedule()
            finally:
                self.shutdown_children()
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)

    def schedule(self):
        start = time.monotonic()
        initial = self.monitor.sample()
        ceiling = min(
            len(self.rows),
            self.manifest["max_parallel"],
            max(1, int(self.monitor.cpus / self.manifest["threads_per_episode"])),
        )
        target = 1
        last_adjust = start
        peak_memory, peak_gpu = 0, 0
        base_memory = initial["memory_bytes"]
        previous_rate = None
        previous_units = sum(
            r.get("work_units", 0) for r in self.rows.values() if r["status"] == "complete"
        )
        initial_complete = sum(r["status"] == "complete" for r in self.rows.values())
        failures_seen = 0
        blocked_since = None
        resource_blocked = False
        with (self.output / "metrics.jsonl").open("a") as metrics:
            while not self.interrupted:
                self.collect()
                pending = [
                    x
                    for x in self.manifest["episodes"]
                    if self.rows[x["id"]]["status"] == "pending"
                ]
                if not pending and not self.active:
                    break
                sample = self.monitor.sample()
                count = len(self.active)
                if count:
                    peak_memory = max(peak_memory, (sample["memory_bytes"] - base_memory) / count)
                    peak_gpu = max(peak_gpu, sample.get("gpu_budget_used_mb", 0) / count)
                now = time.monotonic()
                reason = "warming_up"
                rate = None
                if (
                    now - start >= self.manifest["warmup_seconds"]
                    and now - last_adjust >= self.manifest["adjust_seconds"]
                ):
                    units = sum(
                        r.get("work_units", 0)
                        for r in self.rows.values()
                        if r["status"] == "complete"
                    )
                    rate = (units - previous_units) / (now - last_adjust)
                    # Only compare windows with completions; empty windows in long episodes
                    # cannot establish a throughput regression.
                    regressed = (
                        previous_rate is not None and rate > 0 and rate < previous_rate * 0.8
                    )
                    target, reason = choose_parallelism(
                        target, ceiling, sample, count, peak_memory, peak_gpu, regressed
                    )
                    if rate > 0:
                        previous_rate = rate
                    previous_units, last_adjust = units, now
                failures = sum(r["status"] == "failed" for r in self.rows.values())
                if failures > failures_seen:
                    target = max(1, target // 2)
                    reason = "episode_failure"
                    failures_seen = failures
                metrics.write(
                    json.dumps(
                        dict(
                            sample,
                            elapsed_seconds=now - start,
                            active=count,
                            target=target,
                            ceiling=ceiling,
                            reason=reason,
                            work_units_per_second=rate,
                        )
                    )
                    + "\n"
                )
                metrics.flush()
                # One new process per observation avoids filling several slots from stale
                # headroom measurements while models are still loading.
                if (
                    pending
                    and len(self.active) < target
                    and admission_allowed(sample, len(self.active), peak_memory, peak_gpu)
                ):
                    self.launch(pending.pop(0))
                if pending and not self.active:
                    blocked_since = blocked_since or now
                    if now - blocked_since >= self.manifest["idle_timeout_seconds"]:
                        resource_blocked = True
                        break
                else:
                    blocked_since = None
                time.sleep(min(1, self.manifest["adjust_seconds"] / 5))
        if self.interrupted:
            self.shutdown_children()
        summary = {
            "manifest_sha256": self.manifest_sha,
            "elapsed_seconds": time.monotonic() - start,
            "complete": sum(r["status"] == "complete" for r in self.rows.values()),
            "failed": sum(r["status"] == "failed" for r in self.rows.values()),
            "reused_complete": initial_complete,
            "max_parallel_observed": self.maximum_observed,
            "ceiling": ceiling,
            "interrupted": self.interrupted,
            "resource_blocked": resource_blocked,
        }
        summary["new_episodes_per_second"] = (summary["complete"] - initial_complete) / max(
            summary["elapsed_seconds"], 0.001
        )
        atomic_json(self.output / "summary.json", summary)
        print(json.dumps(summary), flush=True)
        return (
            130
            if self.interrupted
            else (75 if resource_blocked else (1 if summary["failed"] else 0))
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return EpisodeExecutor(args.manifest, args.output).run()


if __name__ == "__main__":
    raise SystemExit(main())
