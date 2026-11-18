"""Offline o200k counts for explicit estimates, not provider-exact billing."""

import json
from functools import lru_cache

MODEL_CONTEXT_LIMIT_EXCEEDED = "model_context_limit_exceeded"


@lru_cache(maxsize=1)
def encoding():
    from litellm.rust_bridge.tokenizer import OpenAIEncoding

    return OpenAIEncoding.from_tiktoken("o200k_base")


def count_tokens(value) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(encoding().encode_ordinary(text))


def replayed_input(items):
    """Return the saved input items an upstream actually receives.

    Provider-internal reasoning items are removed before forwarding history; they are
    never billed as input. Budget checks must use the same view or every estimate is
    needlessly inflated and compaction starts far too early.
    """
    if not isinstance(items, list):
        return items
    return [
        item
        for item in items
        if not (isinstance(item, dict) and item.get("type") == "reasoning")
    ]


def estimate_request_tokens(arguments: dict, provider_reserve: int = 4096) -> int:
    messages = arguments.get("input", [])
    counted = {**arguments, "input": replayed_input(messages)}
    message_count = len(counted["input"]) if isinstance(counted["input"], list) else 1
    return (
        count_tokens(counted)
        + provider_reserve
        + 16
        + 16 * message_count
        + 16 * len(arguments.get("tools") or [])
    )
