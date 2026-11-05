import pytest

from a3_research_agent.host.bootstrap import validate_remote_health


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"status": "error", "error": "RuntimeError"},
        {"status": "ok", "protocol_version": 2},
    ],
)
def test_unhealthy_controller_cannot_pass_preflight(response):
    with pytest.raises(RuntimeError, match="Remote controller"):
        validate_remote_health(response)


def test_known_controller_protocol_passes_preflight():
    validate_remote_health({"status": "ok", "protocol_version": 1})
