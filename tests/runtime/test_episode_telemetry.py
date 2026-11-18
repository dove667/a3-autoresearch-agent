"""Verify GPU telemetry with recorded driver measurements."""

import json
import subprocess

import pytest

from a3_research_agent.execution.controller import episode_telemetry as telemetry
from a3_research_agent.execution.controller import processes


@pytest.fixture(autouse=True)
def owned_processes(monkeypatch):
    monkeypatch.setattr(processes, "process_ids", lambda job: [101, 102])


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
            "gpu_uuid": "GPU-selected",
            "gpu_memory_mb": 800,
        }
    )
    assert sample["gpu_budget_used_mb"] == 500
    assert sample["gpu_used_mb"] == 3000
    assert sample["gpu_limit_mb"] == 800
    assert sample["gpu_accounting"] == "owned_compute_and_graphics_plus_unattributed"
    assert "101" not in json.dumps(sample) and "999" not in json.dumps(sample)
    assert all("GPU-selected" in command for command in calls)


def test_graphics_only_memory_is_not_reported_as_zero(monkeypatch):
    outputs = iter(["150, 24000, 5\n", process_xml([])])
    monkeypatch.setattr(telemetry, "command_output", lambda _: next(outputs))
    sample = telemetry.sample_gpu_resources(
        {
            "gpu_uuid": "GPU-selected",
            "gpu_memory_mb": 512,
        }
    )
    assert sample["gpu_budget_used_mb"] == 150


def test_other_jobs_graphics_are_not_charged_to_this_job(monkeypatch):
    outputs = iter(
        [
            "2000, 24000, 5\n",
            process_xml([(101, 200), (999, 1700)]),
        ]
    )
    monkeypatch.setattr(telemetry, "command_output", lambda _: next(outputs))
    sample = telemetry.sample_gpu_resources({"gpu_uuid": "GPU-test", "gpu_memory_mb": 1024})
    assert sample["gpu_budget_used_mb"] == 300  # Own 200 + unassigned 100.


def test_missing_xml_memory_falls_back_to_conservative_accounting(monkeypatch):
    outputs = iter(
        [
            "2000, 24000, 5\n",
            process_xml([(101, "N/A")]),
            "101, 200\n",
        ]
    )
    monkeypatch.setattr(telemetry, "command_output", lambda _: next(outputs))
    sample = telemetry.sample_gpu_resources({"gpu_uuid": "GPU-test", "gpu_memory_mb": 1024})
    assert sample["gpu_budget_used_mb"] == 2000
    assert sample["gpu_accounting"] == "conservative_graphics_upper_bound"


@pytest.mark.parametrize("pids", [[], [101]])
def test_unavailable_samples_publish_generic_error(tmp_path, monkeypatch, pids):
    monkeypatch.setattr(processes, "process_ids", lambda job: pids)

    def failed_output(arguments):
        raise subprocess.CalledProcessError(1, arguments, stderr="private-host-information")

    monkeypatch.setattr(telemetry, "command_output", failed_output)
    sampler = telemetry.EpisodeTelemetry(
        {
            "episode_telemetry_directory": str(tmp_path),
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
