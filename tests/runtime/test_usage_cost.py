"""Cost follows the operator's rates and stays absent when they are unknown."""

import json

from a3_research_agent.cli import usage_cost
from a3_research_agent.host.models import pricing

USAGE = {
    "input_tokens": 1_000_000,
    "output_tokens": 100_000,
    "input_tokens_details": {"cached_tokens": 750_000},
}
RATES = {
    "A3_PRICE_INPUT_PER_MTOK": "3",
    "A3_PRICE_CACHED_INPUT_PER_MTOK": "0.3",
    "A3_PRICE_OUTPUT_PER_MTOK": "15",
}


def test_cost_uses_the_cached_rate_for_cached_input():
    # 250k fresh at 3, 750k cached at 0.3, 100k output at 15
    assert pricing.cost(USAGE, RATES) == round((0.75 + 0.225 + 1.5), 6)


def test_cached_falls_back_to_the_input_rate():
    rates = {"A3_PRICE_INPUT_PER_MTOK": "3", "A3_PRICE_OUTPUT_PER_MTOK": "15"}
    # 1M input all at 3 (cache inherits the input rate), 100k output at 15
    assert pricing.cost(USAGE, rates) == round((3.0 + 1.5), 6)


def test_missing_rates_leave_the_cost_unknown():
    assert pricing.cost(USAGE, {}) is None


def test_invalid_rates_are_ignored():
    assert pricing.cost(USAGE, {"A3_PRICE_INPUT_PER_MTOK": "abc", "A3_PRICE_OUTPUT_PER_MTOK": "1"}) is None


def test_totals_group_by_role_and_keep_the_currency(tmp_path, monkeypatch):
    # Another test may have loaded the deployment .env, which prices new entries.
    for key in ("A3_PRICE_INPUT_PER_MTOK", "A3_PRICE_CACHED_INPUT_PER_MTOK",
                "A3_PRICE_OUTPUT_PER_MTOK"):
        monkeypatch.setenv(key, "")
    journal = tmp_path / "model-usage.jsonl"
    rows = [
        {"model_role": "main", "usage": USAGE, "cost": 2.475, "cost_currency": "usd"},
        {"model_role": "main", "usage": USAGE, "cost": 2.475, "cost_currency": "usd"},
        {"model_role": "summary", "usage": USAGE},
    ]
    journal.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    result = usage_cost.totals(journal)
    assert result["currency"] == "usd"
    assert result["roles"]["main"]["calls"] == 2
    assert result["roles"]["main"]["input_tokens"] == 2_000_000
    assert result["roles"]["main"]["cost"] == 4.95
    assert result["roles"]["summary"]["cost"] == 0


def test_an_unpriced_journal_is_priced_from_its_usage(tmp_path, monkeypatch):
    for key, value in RATES.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("A3_PRICE_CURRENCY", "cny")
    journal = tmp_path / "model-usage.jsonl"
    journal.write_text(json.dumps({"model_role": "main", "usage": USAGE}) + "\n")
    result = usage_cost.totals(journal)
    assert result["currency"] == "cny"
    assert result["roles"]["main"]["cost"] == round(0.75 + 0.225 + 1.5, 6)
