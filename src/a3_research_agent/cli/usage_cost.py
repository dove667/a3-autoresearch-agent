"""Total one run's model usage and cost from its usage journal."""

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from a3_research_agent.host.models import pricing
from a3_research_agent.host.models.roles import AGENT_NAMES, agent_role


def totals(path):
    """Sum tokens and cost per model role; missing prices stay unreported."""
    rows = []
    with Path(path).open() as stream:
        for line in stream:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                rows.append(entry)
    summary = {}
    for entry in rows:
        role = agent_role(entry)
        item = summary.setdefault(
            role,
            {"calls": 0, "input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "cost": 0.0},
        )
        usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
        item["calls"] += 1
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if type(value) is int:
                item[key] = item.get(key, 0) + value
        details = usage.get("input_tokens_details")
        if isinstance(details, dict) and type(details.get("cached_tokens")) is int:
            item["cached_tokens"] += details["cached_tokens"]
        cost = entry.get("cost")
        if not isinstance(cost, (int, float)):
            # Older journals predate priced entries; price them from their usage.
            cost = pricing.cost(usage)
        if isinstance(cost, (int, float)):
            item["cost"] += cost
            item["priced"] = True
    currency = next(
        (entry.get("cost_currency") for entry in rows if entry.get("cost_currency")), None
    )
    if currency is None and any(item.get("priced") for item in summary.values()):
        currency = pricing.currency()
    return {"currency": currency, "roles": summary}


def main() -> None:
    load_dotenv(os.environ.get("A3_RSI_ENV_FILE", ".env"), override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Trajectory directory or its model-usage.jsonl")
    args = parser.parse_args()
    path = args.run / "model-usage.jsonl" if args.run.is_dir() else args.run
    if not path.is_file():
        raise SystemExit(f"No usage journal at {path}")
    result = totals(path)
    currency = result["currency"]
    for role, item in sorted(result["roles"].items()):
        cost = f"{item['cost']:.4f} {currency.upper()}" if currency else "-"
        print(
            f"{AGENT_NAMES.get(role, role):22} calls={item['calls']:5} "
            f"input={item['input_tokens']:>10} cached={item['cached_tokens']:>10} "
            f"output={item['output_tokens']:>8} cost={cost}"
        )
    if result["currency"] is None:
        print("No cost recorded: set A3_PRICE_* per-million rates in .env")


if __name__ == "__main__":
    main()
