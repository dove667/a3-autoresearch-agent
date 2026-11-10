import httpx
import pytest

from a3_research_agent.host.models import upstream


@pytest.mark.parametrize("read_timeout", [None, "300"])
def test_upstream_preserves_tool_history_and_routes_with_host_key(monkeypatch, read_timeout):
    monkeypatch.setenv("A3_UPSTREAM_RESPONSES_BASE_URL", "https://provider.example/v1/")
    monkeypatch.setenv("A3_UPSTREAM_RESPONSES_MODEL", "configured-model")
    monkeypatch.setenv("A3_UPSTREAM_RESPONSES_API_KEY", "test-key")
    timeout_key = "A3_UPSTREAM_RESPONSES_READ_TIMEOUT_SECONDS"
    if read_timeout is None:
        monkeypatch.delenv(timeout_key, raising=False)
    else:
        monkeypatch.setenv(timeout_key, read_timeout)
    call = {"type": "function_call", "call_id": "call_1"}
    result = {"type": "function_call_output", "call_id": "call_1", "output": "done"}
    reasoning = {"type": "reasoning", "encrypted_content": "old-provider"}

    def post(url, *, headers, json, timeout):
        assert url == "https://provider.example/v1/responses"
        assert headers["Authorization"] == "Bearer test-key"
        assert json == {"model": "configured-model", "input": [call, result]}
        assert timeout.connect == 10
        assert timeout.read == int(read_timeout or "120")
        return httpx.Response(
            200, json={"model": "configured-model"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(upstream.httpx, "post", post)
    forward = upstream.configured_upstream(None)
    assert forward(
        model="saved-model", input=[reasoning, call, result], service_tier="default"
    ) == {"model": "configured-model"}


def test_upstream_fails_closed_without_key(monkeypatch):
    monkeypatch.setenv("A3_UPSTREAM_RESPONSES_BASE_URL", "https://provider.example/v1")
    monkeypatch.delenv("A3_UPSTREAM_RESPONSES_API_KEY", raising=False)
    with pytest.raises(ValueError, match="requires its API key"):
        upstream.configured_upstream(None)
