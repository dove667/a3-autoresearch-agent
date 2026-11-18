"""Execute the job contract inside a simulated Controller output directory."""

import os
import subprocess
import sys

import pytest

from a3_research_agent.harness import job_preparation


def request_command(source, contract):
    job = {"command": [sys.executable, "-c", source], "gpu": True, "preflight": contract}
    return job_preparation.production_request(job)["command"]


def run_job(tmp_path, source, contract):
    return subprocess.run(
        request_command(source, contract),
        env={**os.environ, "A3_OUTPUTS": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_declared_outputs_cannot_overwrite_an_existing_result(tmp_path):
    output = tmp_path / "trial"
    output.mkdir()
    marker = output / "weight.pt"
    marker.write_text("original")
    command = f"from pathlib import Path; Path({str(marker)!r}).write_text('overwritten')"
    result = run_job(tmp_path, command, {"output_paths": ["trial"]})
    assert result.returncode != 0
    assert "output_collision" in result.stderr
    assert marker.read_text() == "original"


def test_concurrent_jobs_cannot_use_overlapping_output_namespaces(tmp_path):
    first_command = request_command(
        "print('ready', flush=True); input()", {"output_paths": ["trial/subrun"]}
    )
    with subprocess.Popen(
        first_command,
        env={**os.environ, "A3_OUTPUTS": str(tmp_path)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as first:
        try:
            assert first.stdout.readline().strip() == "ready"
            second = run_job(tmp_path, "pass", {"output_paths": ["trial"]})
            assert second.returncode != 0
            assert "output_collision" in second.stderr
        finally:
            first.communicate("\n", timeout=3)


def test_a_successful_command_must_publish_its_declared_checkpoint(tmp_path):
    contract = {"output_paths": ["trial"], "checkpoint_paths": ["trial/weights.pt"]}
    result = run_job(tmp_path, "pass", contract)
    assert result.returncode != 0
    assert "checkpoint_missing" in result.stderr
    result = run_job(
        tmp_path,
        "from pathlib import Path; import os; p = Path(os.environ['A3_OUTPUTS']) / 'trial'; "
        "p.mkdir(); (p / 'weights.pt').write_text('weights')",
        contract,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "trial/weights.pt").read_text() == "weights"


@pytest.mark.parametrize("paths", [["../trial"], ["/trial"], ["a", "a/b"], ["a", "a"]])
def test_output_contract_rejects_unsafe_and_overlapping_targets(paths):
    with pytest.raises(ValueError):
        job_preparation.normalize_preflight({"output_paths": paths})
