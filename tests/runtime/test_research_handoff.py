"""Research handoffs reject fabricated provenance and changed numeric results."""

import copy
import json

import pytest

from a3_research_agent.harness.context import summary


@pytest.fixture
def observations():
    return [
        {
            "type": "function_call_output",
            "call_id": "eval",
            "output": json.dumps({"job_id": "a" * 32, "status": "completed", "accuracy": 0.42}),
        }
    ]


def document(observations):
    value = {"version": 1, **{key: [] for key in summary.SECTIONS}}
    value["results"] = [
        {
            "text": "Accuracy 0.42 for job " + "a" * 32,
            "source": 1,
            "quote": observations[0]["output"],
        }
    ]
    return value


def test_handoff_keeps_exact_metric_identifier_and_source(observations):
    value, rendered, report = summary.parse(json.dumps(document(observations)), observations)
    assert "0.42" in rendered and "a" * 32 in rendered
    assert report == {"version": 1, "entries": 1, "cited_messages": 1}
    assert value["results"][0]["source"] == 1


@pytest.mark.parametrize("replacement", ["Accuracy 0.82", "Accuracy 0.4", "job " + "b" * 32])
def test_handoff_rejects_mutated_numbers_or_job_ids(observations, replacement):
    value = document(observations)
    value["results"][0]["text"] = replacement
    with pytest.raises(summary.SummaryValidationError, match="unsupported"):
        summary.validate(value, observations)


@pytest.mark.parametrize(
    "field,value", [("source", 2), ("source", True), ("quote", "accuracy: 0.82"), ("quote", "")]
)
def test_handoff_requires_real_source_excerpt(observations, field, value):
    payload = document(observations)
    payload["results"][0][field] = value
    with pytest.raises(summary.SummaryValidationError):
        summary.validate(payload, observations)


def test_handoff_requires_all_sections_and_at_least_one_supported_item(observations):
    payload = document(observations)
    del payload["failures"]
    with pytest.raises(summary.SummaryValidationError, match="schema_fields"):
        summary.validate(payload, observations)
    payload = {"version": 1, **{key: [] for key in summary.SECTIONS}}
    with pytest.raises(summary.SummaryValidationError, match="all_sections_empty"):
        summary.validate(payload, observations)


def test_render_roundtrip_exposes_quotes_instead_of_hiding_validation(observations):
    payload = document(observations)
    before = copy.deepcopy(payload)
    text = summary.render(payload)
    assert "Exact source:" in text and "[source 1]" in text
    assert payload == before


@pytest.mark.parametrize(
    "original,claim",
    [
        ("lr=1e-3", "lr=1e-2"),
        ("准确率0.42", "准确率0.82"),
        ("任务2×10", "任务2×5"),
        ("accuracy=0.42", "accuracy=.82"),
        ("loss=-0.42", "loss=-.82"),
        ("checkpoint_epoch_100", "checkpoint_epoch_999"),
    ],
)
def test_numeric_tokens_cover_scientific_notation_and_chinese(original, claim):
    payload = {"version": 1, **{key: [] for key in summary.SECTIONS}}
    payload["results"] = [{"text": claim, "source": 1, "quote": original}]
    with pytest.raises(summary.SummaryValidationError, match="unsupported_number"):
        summary.validate(payload, [{"role": "user", "content": original}])


def configure_manager(monkeypatch, tmp_path):
    from a3_research_agent.harness.context import manager

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        manager,
        "TOKEN_CONFIG",
        {
            "target_tokens": 10000,
            "summary_input_tokens": 100000,
            "summary_reserve_tokens": 1000,
            "summary_context_tokens": 120000,
        },
    )
    monkeypatch.setattr(manager.time, "sleep", lambda seconds: None)
    return manager


def response(text):
    return {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


def test_numeric_mutation_is_repaired_before_publication(monkeypatch, tmp_path, observations):
    manager = configure_manager(monkeypatch, tmp_path)
    original = copy.deepcopy(observations)
    valid = document(observations)
    invalid = copy.deepcopy(valid)
    invalid["results"][0]["text"] = "Accuracy 0.82"
    calls = []

    def provider(method, path, body):
        calls.append(copy.deepcopy(body))
        return response(json.dumps(invalid if len(calls) == 1 else valid))

    source = manager.archive(observations, "snapshot")
    rendered, usage, _ = manager.full_summary(observations, source, provider)
    assert len(calls) == 2 and "0.42" in rendered and "0.82" not in rendered
    assert json.loads(calls[0]["transcript"]) == original
    repair = json.loads(calls[1]["transcript"])
    assert repair[:-1] == original
    assert repair[-1]["type"] == "summary_validation_feedback"
    assert "unsupported_number" in repair[-1]["error"]
    assert usage["a3_summary_attempts"] == 2
    assert observations == original
    replay, _, _ = manager.full_summary(
        observations, source, lambda *args: pytest.fail("cached summary replayed")
    )
    assert replay == rendered


def test_invalid_summary_never_replaces_checkpoint(monkeypatch, tmp_path, observations):
    manager = configure_manager(monkeypatch, tmp_path)
    invalid = document(observations)
    invalid["results"][0]["quote"] = "accuracy 1.0"
    source = manager.archive(observations, "snapshot")
    calls = []

    def provider(*args):
        calls.append(args)
        return response(json.dumps(invalid))

    with pytest.raises(manager.ContextError, match="quote_not_in_source"):
        manager.full_summary(observations, source, provider)
    assert len(calls) == 3
    with pytest.raises(manager.ContextError, match="bounded attempts"):
        manager.full_summary(observations, source, provider)
    assert len(calls) == 3
    from a3_research_agent.harness import state_paths

    assert json.loads(state_paths.path(source).read_text()) == observations


def test_feedback_metadata_cannot_be_used_as_evidence(monkeypatch, tmp_path, observations):
    manager = configure_manager(monkeypatch, tmp_path)
    value = document(observations)
    value["results"][0]["source"] = 2
    source = manager.archive(observations, "snapshot")
    with pytest.raises(manager.ContextError, match="source_index_invalid"):
        manager.full_summary(observations, source, lambda *args: response(json.dumps(value)))
