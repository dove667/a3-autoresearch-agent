import subprocess

import pytest

from a3_research_agent.host import remote_client


def test_remote_call_reports_the_ssh_diagnostic(monkeypatch):
    for name, value in {
        "A3_REMOTE_HOST": "fixture-host",
        "A3_REMOTE_CONTROLLER": "/data/controller",
        "A3_REMOTE_RUN_ROOT": "/data/runs",
    }.items():
        monkeypatch.setenv(name, value)

    def fail(command, **options):
        raise subprocess.CalledProcessError(
            255,
            command,
            "",
            "ssh: connect to host fixture-host port 22: Network is unreachable",
        )

    monkeypatch.setattr(remote_client.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError) as error:
        remote_client.remote_call("health", {})
    assert isinstance(error.value, remote_client.RemoteCommandError)
    assert error.value.returncode == 255
    assert str(error.value) == (
        "ssh exit status 255: ssh: connect to host fixture-host port 22: Network is unreachable"
    )
