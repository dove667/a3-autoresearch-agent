"""Price a model call from operator-supplied per-million rates.

Providers return token counts but not always a price, so cost stayed null and a
night of research could only be described in tokens. These rates live in the
deployment `.env`; when they are absent the cost stays null instead of inventing a
number.
"""

import os
from collections.abc import Mapping

MILLION = 1_000_000


def _rate(environment, name):
    raw = environment.get(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def rates(environment: Mapping[str, str] | None = None):
    """Read the three rates; cached input falls back to the input rate."""
    environment = os.environ if environment is None else environment
    input_rate = _rate(environment, "A3_PRICE_INPUT_PER_MTOK")
    cached_rate = _rate(environment, "A3_PRICE_CACHED_INPUT_PER_MTOK")
    output_rate = _rate(environment, "A3_PRICE_OUTPUT_PER_MTOK")
    return {
        "input": input_rate,
        "cached": cached_rate if cached_rate is not None else input_rate,
        "output": output_rate,
    }


def currency(environment: Mapping[str, str] | None = None) -> str:
    environment = os.environ if environment is None else environment
    value = (environment.get("A3_PRICE_CURRENCY") or "usd").strip().lower()
    return value if value.isalpha() and len(value) <= 8 else "usd"


def cost(usage, environment: Mapping[str, str] | None = None):
    """Return the cost of one usage block, or None when a needed rate is missing."""
    if not isinstance(usage, dict):
        return None
    table = rates(environment)
    if table["input"] is None or table["output"] is None:
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if type(input_tokens) is not int or type(output_tokens) is not int:
        return None
    details = usage.get("input_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    cached = cached if type(cached) is int and 0 <= cached <= input_tokens else 0
    fresh = input_tokens - cached
    total = (
        fresh * table["input"] + cached * table["cached"] + output_tokens * table["output"]
    ) / MILLION
    return round(total, 6)
