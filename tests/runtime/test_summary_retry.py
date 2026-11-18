"""Summary retries retain their source and survive operator repairs."""

import json

import pytest

from a3_research_agent.harness import state_paths
from a3_research_agent.harness.context import manager as cm
from tests.runtime.summary_fixtures import handoff_from_request


@pytest.fixture
def setup_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("A3_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        cm,
        "TOKEN_CONFIG",
        {
            "summary_input_tokens": 491808,
            "summary_reserve_tokens": 5000,
            "summary_context_tokens": 524288,
            "target_tokens": 32000,
            "model_routes_sha256": "original-route",
        },
    )
    now = [1000.0]
    monkeypatch.setattr(cm.time, "time", lambda: now[0])
    messages = [{"role": "user", "content": "accuracy=0.42"}]
    return now, messages, cm.archive(messages, "snapshot")


def completed(body):
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": handoff_from_request(body),
                    }
                ],
            }
        ],
    }


def test_temporary_failures_have_no_attempt_ceiling_and_reuse_source(setup_retry):
    now, messages, source = setup_retry
    calls = []

    def provider(method, path, body, **kwargs):
        calls.append((body, kwargs))
        return {"http_error": 503} if len(calls) < 14 else completed(body)

    for attempt in range(13):
        with pytest.raises(cm.SummaryRetry) as waiting:
            cm.full_summary(messages, source, provider)
        assert len(calls) == attempt + 1
        with pytest.raises(cm.SummaryRetry):
            cm.full_summary(messages, source, provider)
        assert len(calls) == attempt + 1
        now[0] += waiting.value.retry_after
    text, usage, _ = cm.full_summary(messages, source, provider)
    assert text and usage["a3_summary_attempts"] == 14
    assert now[0] - 1000 > 300
    assert all(body["source"] == source and options["timeout"] == 960 for body, options in calls)


@pytest.mark.parametrize("status", [401, 403])
def test_credentials_can_recover_without_route_change(setup_retry, status):
    now, messages, source = setup_retry
    calls = []

    def provider(method, path, body, **kwargs):
        calls.append(body)
        return {"http_error": status} if len(calls) == 1 else completed(body)

    with pytest.raises(cm.SummaryRetry) as waiting:
        cm.full_summary(messages, source, provider)
    assert waiting.value.blocked and waiting.value.retry_after == 60
    with pytest.raises(cm.SummaryRetry):
        cm.full_summary(messages, source, provider)
    assert len(calls) == 1
    now[0] += 60
    assert cm.full_summary(messages, source, provider)[0]


def test_permanent_failure_rechecks_after_contract_repair(setup_retry):
    _, messages, source = setup_retry
    calls = []

    def invalid(method, path, body, **kwargs):
        calls.append(body)
        return {"http_error": 422}

    with pytest.raises(cm.ContextError):
        cm.full_summary(messages, source, invalid)
    with pytest.raises(cm.ContextError):
        cm.full_summary(messages, source, invalid)
    assert len(calls) == 1
    cm.TOKEN_CONFIG["model_routes_sha256"] = "repaired-route"
    result, _, _ = cm.full_summary(messages, source, lambda *args, **kwargs: completed(args[-1]))
    assert result
    assert list(state_paths.path(cm.ROOT).glob("superseded-summary-request-*"))


def test_transport_errors_do_not_spend_handoff_repair_budget(setup_retry):
    now, messages, source = setup_retry
    attempts = []

    def provider(method, path, body, **kwargs):
        attempts.append(body)
        if len(attempts) == 1:
            return {"http_error": 502}
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "invalid handoff",
                        }
                    ],
                }
            ],
        }

    with pytest.raises(cm.SummaryRetry) as waiting:
        cm.full_summary(messages, source, provider)
    now[0] += waiting.value.retry_after
    with pytest.raises(cm.ContextError, match="invalid_json"):
        cm.full_summary(messages, source, provider)
    assert len(attempts) == 4
    state = json.loads(next(state_paths.path(cm.ROOT).glob("summary-request-*")).read_text())
    assert state["validation_attempts"] == 3 and state["attempts"] == 4


def test_summary_retries_truncated_text_with_more_output_room(setup_retry):
    _, messages, source = setup_retry
    allowances = []

    def provider(method, path, body, **kwargs):
        allowances.append(body["max_output_tokens"])
        if len(allowances) == 1:
            return {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "{"}]}],
            }
        return completed(body)

    text, usage, _ = cm.full_summary(messages, source, provider, output_tokens=300)
    assert text and usage["a3_summary_attempts"] == 2
    assert allowances == [300, 600]


def test_new_fit_attempt_reissues_a_previously_completed_summary(setup_retry):
    _, messages, source = setup_retry
    calls = []

    def provider(method, path, body, **kwargs):
        calls.append(body)
        return completed(body)

    first, _, _ = cm.full_summary(messages, source, provider)
    replacement, _, _ = cm.full_summary(messages, source, provider, summary_attempt_id="new-fit")
    assert first == replacement
    assert len(calls) == 2 and calls[0] == calls[1]
