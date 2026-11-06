"""Host telemetry tests require neither a GPU nor a Docker daemon."""

import json
import subprocess
from argparse import Namespace

import pytest

from a3_research_agent.execution.controller import docker as job_docker
from a3_research_agent.execution.controller import episode_telemetry as telemetry
from a3_research_agent.execution.controller import lifecycle
from a3_research_agent.execution.controller.store import JobStore


def process_xml(entries):
    return (
        "<nvidia_smi_log><gpu><processes>"
        + "".join(
            f"<process_info><pid>{pid}</pid><used_memory>{memory} MiB</used_memory></process_info>"
            for pid, memory in entries
        )
        + "</processes></gpu></nvidia_smi_log>"
    )


def test_shared_gpu_accounts_owned_compute_and_unknown_graphics(monkeypatch):
    outputs = iter(
        [
            "PID\n101\n102\n",
            "3000, 24000, 75\n",
            process_xml([(101, 100), (102, 200), (999, 2500)]),
        ]
    )
    calls = []

    def output(arguments):
        calls.append(arguments)
        return next(outputs)

    monkeypatch.setattr(telemetry, "command_output", output)
    sample = telemetry.sample_gpu_resources(
        {
            "container_id": "container",
            "gpu_uuid": "GPU-selected",
            "gpu_memory_mb": 800,
        }
    )
    assert sample["gpu_budget_used_mb"] == 500
    assert sample["gpu_used_mb"] == 3000
    assert sample["gpu_limit_mb"] == 800
    assert sample["gpu_accounting"] == "owned_compute_and_graphics_plus_unattributed"
    assert "101" not in json.dumps(sample) and "999" not in json.dumps(sample)
    assert all("GPU-selected" in command for command in calls[1:])


def test_graphics_only_memory_is_not_reported_as_zero(monkeypatch):
    outputs = iter(["PID\n101\n", "150, 24000, 5\n", process_xml([])])
    monkeypatch.setattr(telemetry, "command_output", lambda _: next(outputs))
    sample = telemetry.sample_gpu_resources(
        {
            "container_id": "container",
            "gpu_uuid": "GPU-selected",
            "gpu_memory_mb": 512,
        }
    )
    assert sample["gpu_budget_used_mb"] == 150


def test_other_jobs_graphics_are_not_charged_to_this_job(monkeypatch):
    outputs = iter(
        [
            "PID\n101\n",
            "2000, 24000, 5\n",
            process_xml([(101, 200), (999, 1700)]),
        ]
    )
    monkeypatch.setattr(telemetry, "command_output", lambda _: next(outputs))
    sample = telemetry.sample_gpu_resources(
        {"container_id": "container", "gpu_uuid": "GPU-test", "gpu_memory_mb": 1024}
    )
    assert sample["gpu_budget_used_mb"] == 300  # Own 200 + unassigned 100.


def test_missing_xml_memory_falls_back_to_conservative_accounting(monkeypatch):
    outputs = iter(
        [
            "PID\n101\n",
            "2000, 24000, 5\n",
            process_xml([(101, "N/A")]),
            "101, 200\n",
        ]
    )
    monkeypatch.setattr(telemetry, "command_output", lambda _: next(outputs))
    sample = telemetry.sample_gpu_resources(
        {"container_id": "container", "gpu_uuid": "GPU-test", "gpu_memory_mb": 1024}
    )
    assert sample["gpu_budget_used_mb"] == 2000
    assert sample["gpu_accounting"] == "conservative_graphics_upper_bound"


@pytest.mark.parametrize("output", ["", "PID\n", "PID\n101\n"])
def test_unavailable_samples_publish_generic_error(tmp_path, monkeypatch, output):
    def failed_output(arguments):
        if arguments[0] == "docker":
            return output
        raise subprocess.CalledProcessError(1, arguments, stderr="private-host-information")

    monkeypatch.setattr(telemetry, "command_output", failed_output)
    sampler = telemetry.EpisodeTelemetry(
        {
            "episode_telemetry_directory": str(tmp_path),
            "container_id": "container",
            "gpu_uuid": "GPU-selected",
            "gpu_memory_mb": 512,
        }
    )
    sampler.publish()
    sample = json.loads((tmp_path / "resources.json").read_text())
    assert set(sample) == {"sampled_at", "error"}
    assert "private-host-information" not in json.dumps(sample)


def test_daemon_stops_and_joins_after_publish(tmp_path, monkeypatch):
    sampler = telemetry.EpisodeTelemetry({"episode_telemetry_directory": str(tmp_path)})

    def publish():
        sampler.stop_event.set()

    monkeypatch.setattr(sampler, "publish", publish)
    sampler.start()
    sampler.stop()
    assert not sampler.thread.is_alive()


@pytest.mark.parametrize("gpu", [False, True])
def test_telemetry_mount_is_readonly_and_outside_agent_writable_roots(tmp_path, monkeypatch, gpu):
    store = JobStore(tmp_path / "runtime")
    job = store.create("owner", ["true"], gpu, {})
    job.update(gpu_uuid="GPU-selected" if gpu else None, gpu_memory_mb=512 if gpu else 0)
    calls = []

    def docker(arguments):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "container", "")

    monkeypatch.setattr(job_docker, "docker", docker)
    job_docker.create_container(
        store, job, Namespace(cpus=2, memory="1g", pids_limit=128, image="test")
    )
    saved = store.load(job["job_id"], "owner")
    mounts = [argument for argument in calls[0] if "target=/run/a3/telemetry" in argument]
    if gpu:
        directory = store.metadata_path(job["job_id"]).parent / "episode-telemetry"
        assert saved["episode_telemetry_directory"] == str(directory)
        assert directory.is_dir()
        assert mounts == [f"type=bind,source={directory},target=/run/a3/telemetry,readonly"]
        assert not directory.is_relative_to(store.workspace(job))
        assert not directory.is_relative_to(saved["temporary_directory"])
    else:
        assert not mounts and "episode_telemetry_directory" not in saved


@pytest.mark.parametrize("wait_fails", [False, True])
def test_watcher_always_stops_gpu_sampler(tmp_path, monkeypatch, wait_fails):
    store = JobStore(tmp_path / "runtime")
    job = store.create("owner", ["true"], True, {})
    job.update(
        container_id="container", gpu_uuid="GPU-selected", episode_telemetry_directory=str(tmp_path)
    )
    store.save(job)
    events = []

    class Sampler:
        def __init__(self, job):
            assert job["container_id"] == "container"

        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

    def wait(*args, **kwargs):
        if wait_fails:
            raise subprocess.TimeoutExpired("docker wait", 1)
        return subprocess.CompletedProcess(args, 0, "0", "")

    monkeypatch.setattr(lifecycle, "EpisodeTelemetry", Sampler)
    monkeypatch.setattr(lifecycle, "docker", lambda *args, **kwargs: None)
    monkeypatch.setattr(lifecycle.subprocess, "run", wait)
    monkeypatch.setattr(lifecycle, "container_state", lambda _: {"Status": "exited", "ExitCode": 0})
    monkeypatch.setattr(lifecycle, "reclaim_terminal", lambda *args: None)
    lifecycle.watch(Namespace(root=store.root, job_id=job["job_id"], timeout=1, lock_fd=None))
    assert events == ["start", "stop"]
    assert store.load(job["job_id"], "owner")["status"] == ("failed" if wait_fails else "completed")
