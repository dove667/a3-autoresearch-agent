from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest

from a3_research_agent.host.models import upstream


def test_chatgpt_replays_external_tool_history_without_changing_checkpoint(monkeypatch):
    monkeypatch.delenv("A3_UPSTREAM_RESPONSES_BASE_URL", raising=False)
    reasoning = {"type": "reasoning", "encrypted_content": "native"}
    message = {"type": "message", "id": "msg_native", "role": "assistant"}
    call = {
        "type": "function_call",
        "id": "call_old",
        "call_id": "call_old",
        "name": "read",
        "arguments": "{}",
        "namespace": None,
    }
    result = {"type": "function_call_output", "call_id": "call_old", "output": "done"}
    history = [
        {"type": "reasoning", "role": "assistant", "content": []},
        {
            "type": "message",
            "id": "external_id",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Earlier work"}],
        },
        call,
        result,
        reasoning,
        message,
    ]
    checkpoint = deepcopy(history)
    default = Mock()
    forward = upstream.configured_upstream(default)
    forward(model="chatgpt/example", input=history)
    assert default.call_args.kwargs["input"] == [
        {"role": "assistant", "content": "Earlier work"},
        {key: call[key] for key in ("type", "call_id", "name", "arguments")},
        result,
        reasoning,
        message,
    ]
    assert history == checkpoint
    forward(model="other/example", input=history)
    assert default.call_args.kwargs["input"] == checkpoint


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
